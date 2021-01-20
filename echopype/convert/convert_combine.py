"""
This file contains functions used by Convert.combine_files().
"""

import os
import shutil
from datetime import datetime as dt
from fsspec.mapping import FSMap
from fsspec.implementations.local import LocalFileSystem
import xarray as xr
from .._version import get_versions
from ..utils import io

ECHOPYPE_VERSION = get_versions()['version']

DEFAULT_CHUNK_SIZE = {
    'range_bin': 25000,
    'ping_time': 2500
}


def get_combined_save_path(source_paths):
    def get_combined_fname(path):
        fname, ext = os.path.splitext(path)
        return fname + '__combined' + ext

    sample_file = source_paths[0]
    save_path = get_combined_fname(sample_file.root) if isinstance(sample_file, FSMap) else get_combined_fname \
        (sample_file)
    if isinstance(sample_file, FSMap) and not isinstance(sample_file.fs, LocalFileSystem):
        return sample_file.fs.get_mapper(save_path)
    return save_path


def remove_indiv_files(path):
    """Used to delete .nc or .zarr files"""
    if isinstance(path, FSMap):
        path.fs.delete(path.root, recursive=True)
    else:
        fname, ext = os.path.splitext(path)
        if ext == '.zarr':
            shutil.rmtree(path)
        else:
            os.remove(path)


def assemble_combined_provenance(input_paths):
    prov_dict = {'conversion_software_name': 'echopype',
                 'conversion_software_version': ECHOPYPE_VERSION,
                 'conversion_time': dt.utcnow().isoformat(timespec='seconds') + 'Z',  # use UTC time
                 'src_filenames': input_paths}
    ds = xr.Dataset()
    return ds.assign_attrs(prov_dict)


def perform_combination(sonar_model, input_paths, output_path, engine):
    """Opens a list of Netcdf/Zarr files as a single dataset and saves it to a single file.
    """

    def coerce_type(ds, group):
        if group == 'Beam':
            if sonar_model == 'EK80':
                ds['transceiver_software_version'] = ds['transceiver_software_version'].astype('<U10')
                ds['channel_id'] = ds['channel_id'].astype('<U50')
            elif sonar_model == 'EK60':
                ds['gpt_software_version'] = ds['gpt_software_version'].astype('<U10')
                ds['channel_id'] = ds['channel_id'].astype('<U50')

    print(f"{dt.now().strftime('%H:%M:%S')}  combining files...")

    # TODO: add in the documentation that the Top-level and Sonar groups are
    #  combined by taking values (attributes) from the first file
    # Combine Top-level group, use values from the first file
    with xr.open_dataset(input_paths[0], engine=engine) as ds_top:
        io.save_file(ds_top, path=output_path, mode='w', engine=engine)

    # Combine Sonar group, use values from the first file
    with xr.open_dataset(input_paths[0], group='Sonar', engine=engine) as ds_sonar:
        io.save_file(ds_sonar, path=output_path, mode='a', engine=engine, group='Sonar')

    # Combine Provenance group,
    ds_prov = assemble_combined_provenance(input_paths)
    io.save_file(ds_prov, path=output_path, mode='a', engine=engine, group='Provenance')

    # TODO: Put the following in docs:
    #  Right now we follow xr.combine_by_coords default to only combine files
    #  with nicely monotonically varying ping_time/location_time/mru_time.
    #  However we know there are lots of problems with pings going backward in time for EK60/EK80 files,
    #  and we will need to clean up data before calling merge.
    # Combine Beam
    with xr.open_mfdataset(input_paths, group='Beam',
                           concat_dim='ping_time', data_vars='minimal', engine=engine) as ds_beam:
        coerce_type(ds_beam, 'Beam')
        io.save_file(ds_beam.chunk({'range_bin': DEFAULT_CHUNK_SIZE['range_bin'],
                                    'ping_time': DEFAULT_CHUNK_SIZE['ping_time']}),  # these chunk sizes are ad-hoc
                     path=output_path, mode='a', engine=engine, group='Beam')

    # Combine Environment group
    with xr.open_mfdataset(input_paths, group='Environment',
                           concat_dim='ping_time', data_vars='minimal', engine=engine) as ds_env:
        io.save_file(ds_env.chunk({'ping_time': DEFAULT_CHUNK_SIZE['ping_time']}),
                     path=output_path, mode='a', engine=engine, group='Environment')

    # Combine Platform group
    # The platform group for AZFP does not have coordinates, so it must be handled differently from EK60
    # TODO: check AZFP platform: shouldn't the AZFP platform coordinates just be empty?
    if sonar_model == 'AZFP':
        with xr.open_mfdataset(input_paths, group='Platform', combine='nested',
                               compat='identical', engine=engine) as ds_plat:
            io.save_file(ds_plat, path=output_path, mode='a', engine=engine, group='Platform')
    elif sonar_model in ['EK80', 'EA640']:
        with xr.open_mfdataset(input_paths, group='Platform',
                               concat_dim=['location_time', 'mru_time'],
                               data_vars='minimal', engine=engine) as ds_plat:
            io.save_file(ds_plat.chunk({'location_time': DEFAULT_CHUNK_SIZE['ping_time'],
                                        'mru_time': DEFAULT_CHUNK_SIZE['ping_time']}),
                         path=output_path, mode='a', engine=engine, group='Platform')
    elif sonar_model in ['EK60']:
        with xr.open_mfdataset(input_paths, group='Platform',
                               concat_dim=['location_time', 'ping_time'],
                               data_vars='minimal', engine=engine) as ds_plat:
            io.save_file(ds_plat.chunk({'location_time': DEFAULT_CHUNK_SIZE['ping_time'],
                                        'ping_time': DEFAULT_CHUNK_SIZE['ping_time']}),
                         path=output_path, mode='a', engine=engine, group='Platform')

    # Combine Platform/NMEA group
    if sonar_model in ['EK60', 'EK80', 'EA640']:
        with xr.open_mfdataset(input_paths, group='Platform/NMEA',
                               concat_dim='location_time', data_vars='minimal', engine=engine) as ds_nmea:
            io.save_file(ds_nmea.chunk({'location_time': DEFAULT_CHUNK_SIZE['ping_time']}).astype('str'),
                         path=output_path, mode='a', engine=engine, group='Platform/NMEA')

    # Combine Vendor-specific group
    # TODO: double check this works with AZFP data as data variables change with ping_time
    with xr.open_mfdataset(input_paths, group='Vendor',
                           combine='nested',  # nested since this is more like merge and no dim to concat
                           compat='no_conflicts', data_vars='minimal', engine=engine) as ds_vend:
        io.save_file(ds_vend, path=output_path, mode='a', engine=engine, group='Vendor')

    # TODO: print out which group combination errors out and raise appropriate error

    print(f"{dt.now().strftime('%H:%M:%S')}  all files combined into {output_path}")
