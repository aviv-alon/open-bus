# coding: utf-8

# Using a mix of `partridge` and `gtfstk` with some of my own additions to 
# create daily statistical DataFrames for trips, routes and stops. 
# This will later become a module which we will run on our historical 
# MoT GTFS archive and schedule for nightly runs. 

# ## Imports and config

import pandas as pd
import numpy as np
import datetime
import time
import gtfs_utils as gu
import gtfstk
from collections import OrderedDict, defaultdict
import os
import boto3
import logging
from zipfile import BadZipFile
import itertools
from tqdm import tqdm
from partridge import feed as ptg_feed
from gtfs_stats_conf import *


def compute_trip_stats_partridge(feed, zones):
    """
    Parameters
    ----------
    feed : partridge feed
    zones: DataFrame with stop_code to zone_name mapping
    
    Returns
    -------
    DataFrame with the following columns:

    - ``'trip_id'``
    - ``'route_id'``
    - ``'route_short_name'``
    - ``'route_short_name'``
    - ``'agency_id'``
    - ``'agency_name'``
    - ``'route_long_name'``
    - ``'route_type'``
    - ``'direction_id'``
    - ``'shape_id'``
    - ``'num_stops'``: number of stops on trip
    - ``'start_time'``: first departure time of the trip
    - ``'end_time'``: last departure time of the trip
    - ``'start_stop_id'``: stop ID of the first stop of the trip
    - ``'end_stop_id'``: stop ID of the last stop of the trip
    - ``'start_stop_name'``: stop name of the first stop of the trip
    - ``'end_stop_name'``: stop name of the last stop of the trip
    - ``'start_stop_code'``: stop code of the first stop of the trip
    - ``'end_stop_code'``: stop code of the last stop of the trip
    - ``'start_stop_lat'``: ``start_stop_lat`` of the first stop of the trip
    - ``'start_stop_lon'``: ``start_stop_lon`` of the first stop of the trip
    - ``'end_stop_lat'``: ``end_stop_lat`` of the last stop of the trip
    - ``'end_stop_lon'``: ``end_stop_lon`` of the last stop of the trip
    - ``'start_zone'``: zone name of the first stop of the trip
    - ``'end_zone'``: zone name of the last stop of the trip
    - ``'num_zones'``:  ``num_zones`` of the first stop of the trip
    - ``'num_zones_missing'``:  ``num_zones_missing`` of the first stop of the trip
    - ``'is_loop'``: 1 if the start and end stop are less than 400m apart and
      0 otherwise
    - ``'distance'``: distance of the trip in ``feed.dist_units``;
      contains all ``np.nan`` entries if ``feed.shapes is None``
    - ``'duration'``: duration of the trip in hours
    - ``'speed'``: distance/duration

    TODO: this is not true here, we're only using shape_dist_traveled
    TODO: implement or drop from docs
    If ``feed.stop_times`` has a ``shape_dist_traveled`` column with at
    least one non-NaN value and ``compute_dist_from_shapes == False``,
    then use that column to compute the distance column.
    Else if ``feed.shapes is not None``, then compute the distance
    column using the shapes and Shapely.
    Otherwise, set the distances to NaN.

    If route IDs are given, then restrict to trips on those routes.

    Notes
    -----
    - Assume the following feed attributes are not ``None``:

        * ``feed.trips``
        * ``feed.routes``
        * ``feed.stop_times``
        * ``feed.shapes`` (optionally)
        * Those used in :func:`.stops.build_geometry_by_stop`

    - Calculating trip distances with ``compute_dist_from_shapes=True``
      seems pretty accurate.  For example, calculating trip distances on
      `this Portland feed
      <https://transitfeeds.com/p/trimet/43/1400947517>`_
      using ``compute_dist_from_shapes=False`` and
      ``compute_dist_from_shapes=True``,
      yields a difference of at most 0.83km from the original values.

    """
    f = feed.trips
    f = (f[['route_id', 'trip_id', 'direction_id', 'shape_id']]
         .merge(feed.routes[['route_id', 'route_short_name', 'route_long_name',
                             'route_type', 'agency_id']])
         .merge(feed.agency[['agency_id', 'agency_name']], how='left', on='agency_id')
         .merge(feed.stop_times)
         .merge(feed.stops[['stop_id', 'stop_name', 'stop_lat', 'stop_lon', 'stop_code']])
         .merge(zones, how='left')
         .sort_values(['trip_id', 'stop_sequence'])
         # .assign(departure_time=lambda x: x['departure_time'].map(
         #    hp.timestr_to_seconds)
         #       )
         )
    geometry_by_stop = gtfstk.build_geometry_by_stop(feed, use_utm=True)

    g = f.groupby('trip_id')

    def my_agg(group):
        d = OrderedDict()
        d['route_id'] = group['route_id'].iat[0]
        d['route_short_name'] = group['route_short_name'].iat[0]
        d['route_long_name'] = group['route_long_name'].iat[0]
        d['agency_id'] = group['agency_id'].iat[0]
        d['agency_name'] = group['agency_name'].iat[0]
        d['route_type'] = group['route_type'].iat[0]
        d['direction_id'] = group['direction_id'].iat[0]
        d['shape_id'] = group['shape_id'].iat[0]
        d['num_stops'] = group.shape[0]
        d['start_time'] = group['departure_time'].iat[0]
        d['end_time'] = group['departure_time'].iat[-1]
        d['start_stop_id'] = group['stop_id'].iat[0]
        d['end_stop_id'] = group['stop_id'].iat[-1]
        d['start_stop_code'] = group['stop_code'].iat[0]
        d['end_stop_code'] = group['stop_code'].iat[-1]
        d['start_stop_name'] = group['stop_name'].iat[0]
        d['end_stop_name'] = group['stop_name'].iat[-1]
        d['start_stop_lat'] = group['stop_lat'].iat[0]
        d['start_stop_lon'] = group['stop_lon'].iat[0]
        d['end_stop_lat'] = group['stop_lat'].iat[-1]
        d['end_stop_lon'] = group['stop_lon'].iat[-1]
        d['start_zone'] = group['zone_name'].iat[0]
        d['end_zone'] = group['zone_name'].iat[-1]
        d['num_zones'] = group.zone_name.nunique()
        d['num_zones_missing'] = group.zone_name.isnull().sum()
        dist = geometry_by_stop[d['start_stop_id']].distance(
            geometry_by_stop[d['end_stop_id']])
        d['is_loop'] = int(dist < 400)
        d['duration'] = (d['end_time'] - d['start_time']) / 3600
        return pd.Series(d)

    h = g.apply(my_agg)
    h['distance'] = g.shape_dist_traveled.max()

    # Reset index and compute final stats
    h = h.reset_index()
    h['speed'] = h['distance'] / h['duration'] / 1000
    h[['start_time', 'end_time']] = (
        h[['start_time', 'end_time']].applymap(
            lambda x: gtfstk.helpers.timestr_to_seconds(x, inverse=True))
    )
    return h


def get_active_trips_df(trip_times):
    """
    Count the number of trips in ``trip_times`` that are active
    at any given time.

    Parameters
    ----------
    trip_times : DataFrame
        Contains columns

        - start_time: start time of the trip in seconds past midnight
        - end_time: end time of the trip in seconds past midnight

    Returns
    -------
    Series
        index is times from midnight when trips start and end, values are number of active trips for that time

    """
    active_trips = pd.concat([pd.Series(1, trip_times.start_time),  # departed add 1
                              pd.Series(-1, trip_times.end_time)  # arrived subtract 1
                              ]).groupby(level=0, sort=True).sum().cumsum().ffill()
    return active_trips


def compute_route_stats_base_partridge(trip_stats_subset,
                                       headway_start_time='07:00:00', headway_end_time='19:00:00', *,
                                       split_directions=False):
    """
    Compute stats for the given subset of trips stats.

    Parameters
    ----------
    trip_stats_subset : DataFrame
        Subset of the output of :func:`.trips.compute_trip_stats`
    split_directions : boolean
        If ``True``, then separate the stats by trip direction (0 or 1);
        otherwise aggregate trips visiting from both directions. 
        Default: ``False``
    headway_start_time : string
        HH:MM:SS time string indicating the start time for computing
        headway stats
        Default: ``'07:00:00'``
    headway_end_time : string
        HH:MM:SS time string indicating the end time for computing
        headway stats.
        Default: ``'19:00:00'``

    Returns
    -------
    DataFrame
        Columns are

        - ``'route_id'``
        - ``'route_short_name'``
        - ``'agency_id'``
        - ``'agency_name'``
        - ``'route_long_name'``
        - ``'route_type'``
        - ``'direction_id'``: 1/0
        - ``'num_trips'``: number of trips on the route in the subset
        - ``'num_trip_starts'``: number of trips on the route with
          nonnull start times
        - ``'num_trip_ends'``: number of trips on the route with nonnull
          end times that end before 23:59:59
        - ``'is_loop'``: 1 if at least one of the trips on the route has
          its ``is_loop`` field equal to 1; 0 otherwise
        - ``'is_bidirectional'``: 1 if the route has trips in both
          directions; 0 otherwise
        - ``'start_time'``: start time of the earliest trip on the route
        - ``'end_time'``: end time of latest trip on the route
        - ``'max_headway'``: maximum of the durations (in minutes)
          between trip starts on the route between
          ``headway_start_time`` and ``headway_end_time`` on the given
          dates
        - ``'min_headway'``: minimum of the durations (in minutes)
          mentioned above
        - ``'mean_headway'``: mean of the durations (in minutes)
          mentioned above
        - ``'peak_num_trips'``: maximum number of simultaneous trips in
          service (for the given direction, or for both directions when
          ``split_directions==False``)
        - ``'peak_start_time'``: start time of first longest period
          during which the peak number of trips occurs
        - ``'peak_end_time'``: end time of first longest period during
          which the peak number of trips occurs
        - ``'service_duration'``: total of the duration of each trip on
          the route in the given subset of trips; measured in hours
        - ``'service_distance'``: total of the distance traveled by each
          trip on the route in the given subset of trips; measured in
          whatever distance units are present in ``trip_stats_subset``;
          contains all ``np.nan`` entries if ``feed.shapes is None``
        - ``'service_speed'``: service_distance/service_duration;
          measured in distance units per hour
        - ``'mean_trip_distance'``: service_distance/num_trips
        - ``'mean_trip_duration'``: service_duration/num_trips
        - ``'start_stop_id'``: ``start_stop_id`` of the first trip for the route
        - ``'end_stop_id'``: ``end_stop_id`` of the first trip for the route
        - ``'start_stop_lat'``: ``start_stop_lat`` of the first trip for the route
        - ``'start_stop_lon'``: ``start_stop_lon`` of the first trip for the route
        - ``'end_stop_lat'``: ``end_stop_lat`` of the first trip for the route
        - ``'end_stop_lon'``: ``end_stop_lon`` of the first trip for the route
        - ``'num_stops'``: ``num_stops`` of the first trip for the route
        - ``'start_zone'``: ``start_zone`` of the first trip for the route
        - ``'end_zone'``: ``end_zone`` of the first trip for the route
        - ``'num_zones'``:  ``num_zones`` of the first trip for the route
        - ``'num_zones_missing'``:  ``num_zones_missing`` of the first trip for the route

        TODO: actually implement split_directions
        If not ``split_directions``, then remove the
        direction_id column and compute each route's stats,
        except for headways, using
        its trips running in both directions.
        In this case, (1) compute max headway by taking the max of the
        max headways in both directions; (2) compute mean headway by
        taking the weighted mean of the mean headways in both
        directions.
        
        If ``trip_stats_subset`` is empty, return an empty DataFrame.

    """
    f = trip_stats_subset.copy()
    f[['start_time', 'end_time']] = f[['start_time', 'end_time']].applymap(gtfstk.helpers.timestr_to_seconds)

    headway_start = gtfstk.helpers.timestr_to_seconds(headway_start_time)
    headway_end = gtfstk.helpers.timestr_to_seconds(headway_end_time)

    def compute_route_stats(group):
        d = OrderedDict()
        d['route_short_name'] = group['route_short_name'].iat[0]
        d['route_long_name'] = group['route_long_name'].iat[0]
        d['agency_id'] = group['agency_id'].iat[0]
        d['agency_name'] = group['agency_name'].iat[0]
        d['route_type'] = group['route_type'].iat[0]
        d['num_trips'] = group.shape[0]
        d['num_trip_starts'] = group['start_time'].count()
        d['num_trip_ends'] = group.loc[
            group['end_time'] < 24 * 3600, 'end_time'].count()
        d['is_loop'] = int(group['is_loop'].any())
        d['is_bidirectional'] = int(group['direction_id'].unique().size > 1)
        d['start_time'] = group['start_time'].min()
        d['end_time'] = group['end_time'].max()

        # Compute headway stats
        headways = np.array([])
        for direction in [0, 1]:
            stimes = group[group['direction_id'] == direction][
                'start_time'].values
            stimes = sorted([stime for stime in stimes
                             if headway_start <= stime <= headway_end])
            headways = np.concatenate([headways, np.diff(stimes)])
        if headways.size:
            d['max_headway'] = np.max(headways) / 60  # minutes
            d['min_headway'] = np.min(headways) / 60  # minutes
            d['mean_headway'] = np.mean(headways) / 60  # minutes
        else:
            d['max_headway'] = np.nan
            d['min_headway'] = np.nan
            d['mean_headway'] = np.nan

        # Compute peak num trips
        active_trips = get_active_trips_df(group[['start_time', 'end_time']])
        times, counts = active_trips.index.values, active_trips.values
        start, end = gtfstk.helpers.get_peak_indices(times, counts)
        d['peak_num_trips'] = counts[start]
        d['peak_start_time'] = times[start]
        d['peak_end_time'] = times[end]

        d['service_distance'] = group['distance'].sum()
        d['service_duration'] = group['duration'].sum()

        # Added by cjer
        d['start_stop_id'] = group['start_stop_id'].iat[0]
        d['end_stop_id'] = group['end_stop_id'].iat[0]

        d['start_stop_lat'] = group['start_stop_lat'].iat[0]
        d['start_stop_lon'] = group['start_stop_lon'].iat[0]
        d['end_stop_lat'] = group['end_stop_lat'].iat[0]
        d['end_stop_lon'] = group['end_stop_lon'].iat[0]

        d['num_stops'] = group['num_stops'].iat[0]

        d['start_zone'] = group['start_zone'].iat[0]
        d['end_zone'] = group['end_zone'].iat[0]
        d['num_zones'] = group['num_zones'].iat[0]
        d['num_zones_missing'] = group['num_zones_missing'].iat[0]

        return pd.Series(d)

    g = f.groupby('route_id').apply(
        compute_route_stats).reset_index()

    # Compute a few more stats
    g['service_speed'] = g['service_distance'] / g['service_duration']
    g['mean_trip_distance'] = g['service_distance'] / g['num_trips']
    g['mean_trip_duration'] = g['service_duration'] / g['num_trips']

    # Convert route times to time strings
    time_cols = ['start_time', 'end_time', 'peak_start_time', 'peak_end_time']
    g[time_cols] = g[time_cols].applymap(lambda x: gtfstk.helpers.timestr_to_seconds(x, inverse=True))

    g['service_speed'] = g.service_speed / 1000

    g = g[['route_id', 'route_short_name', 'agency_id', 'agency_name',
           'route_long_name', 'route_type', 'num_trips', 'num_trip_starts',
           'num_trip_ends', 'is_loop', 'is_bidirectional', 'start_time',
           'end_time', 'max_headway', 'min_headway', 'mean_headway',
           'peak_num_trips', 'peak_start_time', 'peak_end_time',
           'service_distance', 'service_duration', 'service_speed',
           'mean_trip_distance', 'mean_trip_duration', 'start_stop_id',
           'end_stop_id', 'start_stop_lat', 'start_stop_lon', 'end_stop_lat',
           'end_stop_lon', 'num_stops', 'start_zone', 'end_zone',
           'num_zones', 'num_zones_missing'
           ]]

    return g


def retry(delays=(0, 1, 5, 30, 180, 600, 3600),
          exception=Exception):
    """
Decorator which performs retries with dynamic intervals set by given delays tuple. Also pulls report kwarg from the
wrapped function call for logging.
    :param delays: tuple of wait time (seconds)
    :type delays: tuple
    :param exception: what exception to catch for retries
    :type exception: type
    :return: wrapped function
    :rtype: function
    """

    def wrapper(func):
        def wrapped(*args, **kwargs):
            report = kwargs['report']
            problems = []
            for delay in itertools.chain(delays, [None]):
                try:
                    return func(*args, **kwargs)
                except exception as problem:
                    problems.append(problem)
                    if delay is None:
                        report("retryable failed definitely:", problems)
                        raise
                    else:
                        report("retryable failed:", problem,
                               "-- delaying for %ds" % delay)
                        time.sleep(delay)

        return wrapped

    return wrapper


@retry()
def s3_download(bucket, key, output_path, report=print):
    """
Download file from s3 bucket. Retry using decorator, and report to logger given in report parameter.
    :param bucket: s3 boto bucket object
    :type bucket: boto3.resources.factory.s3.Bucket
    :param key: key of the file to download
    :type key: str
    :param output_path: output path to download the file to
    :type output_path: str
    :param report: callable to use for logging (e.g. logging logger object)
    :type report: callable
    """

    def hook(t):
        def inner(bytes_amount):
            t.update(bytes_amount)

        return inner

    if DOWNLOAD_PBAR:
        # TODO: this is an S3 anti-pattern, and is inefficient - so defaulting to not doing this
        if SIZE_FOR_DOWNLOAD_PBAR:
            size = [obj.size for obj in bucket.objects.filter(Prefix=key, MaxKeys=1)][0]
        else:
            size = None

        with tqdm(total=size, unit='B', unit_scale=True, desc='download ' + key, leave=True) as t:
            bucket.download_file(key, output_path, Callback=hook(t))
    else:
        bucket.download_file(key, output_path)


def batch_stats(folder=GTFS_FEEDS_PATH, output_folder=OUTPUT_DIR):
    for file in os.listdir(folder):
        date_str = file.split('.')[0]
        date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        feed = gu.get_partridge_feed_by_date(output_folder + file, date)
        zones = gu.get_zones_df(LOCAL_TARIFF_PATH)
        ts = compute_trip_stats_partridge(feed, zones)
        ts.to_pickle(output_folder + date_str + '_trip_stats.pkl.gz', compression='gzip')
        rs = compute_route_stats_base_partridge(ts)
        rs.to_pickle(output_folder + date_str + '_route_stats.pkl.gz', compression='gzip')


def _get_existing_output_files(output_folder):
    """
Get existing output files in the given folder, in a list containing tuples of dates and output types.
    :param output_folder: a folder to check for
    :type output_folder:
    :return: list of 2-tuples (date_str, output_file_type)
    :rtype: list
    """
    return [(g[0], g[1]) for g in
            (re.match(OUTPUT_FILE_NAME_RE, file).groups()
             for file in os.listdir(output_folder))]


def get_bucket_valid_files(bucket_objects):
    """
Get list of valid files from bucket, as set by BUCKET_VALID_FILES_RE
    :param bucket_objects: collection of bucket objects
    :type bucket_objects: s3.Bucket.objectsCollection
    :return: list of valid file keys
    :rtype: list of str
    """
    return [obj.key for obj in bucket_objects
            if re.match(BUCKET_VALID_FILES_RE, obj.key)]


def get_dates_without_output(valid_files, existing_output_files):
    """
Get list of dates without output files (currently just route_stats is considered)
    :param valid_files: list of valid file keys
    :rtype: list of str
    :param existing_output_files: list of 2-tuples as returned by _get_existing_output_files
    :type existing_output_files: list
    :return: list of valid file keys for stat computation
    :rtype: list
    """
    return [parse_date(file)[1] for file in valid_files
            if file not in [g[0] + '.zip'
                            for g in existing_output_files
                            if g[1] == 'route_stats']]


def get_forward_fill_dict(valid_files, future_days=FUTURE_DAYS):
    """
get a dictionary mapping gtfs file names to a list of dates for forward fill by scanning for missing dates for files
    :param valid_files: list of valid file keys
    :rtype: list of str
    :return dictionary mapping file names to dates
    :rtype defaultdict of lists (defaults to empty list)
    """
    existing_dates = pd.DatetimeIndex([parse_date(file)[0] for file in valid_files])
    expected_dates = pd.DatetimeIndex(start=existing_dates.min(), end=existing_dates.max()+datetime.timedelta(days=future_days), freq='D')
    date_df = pd.Series(pd.NaT, expected_dates)
    date_df[existing_dates] = existing_dates
    date_df = date_df.fillna(method='ffill', limit=59)
    # TODO: remove dates that aren't in the 59 day gap
    # BUG: will act unexcpectedly if more than 59 day gap
    ffill = defaultdict(list)
    for file_date, stats_date in zip(date_df.dt.strftime('%Y-%m-%d'), date_df.index.strftime('%Y-%m-%d')):
        ffill[file_date + '.zip'].append(stats_date)

    return ffill


def get_valid_file_dates_dict(bucket_objects, existing_output_files, logger, forward_fill):
    logger.info(f'BUCKET_VALID_FILES_RE={BUCKET_VALID_FILES_RE}')
    bucket_valid_files = get_bucket_valid_files(bucket_objects)
    if forward_fill:
        logger.info(f'applying forward fill')
        ffill_dict = get_forward_fill_dict(bucket_valid_files)
        logger.info(f'found {sum([len(l) for l in ffill_dict.values()]) - len(bucket_valid_files)} missing dates for '
                    'forward fill.')

        files_for_stats = defaultdict(list)
        for file in ffill_dict:
            files_for_stats[file] = get_dates_without_output([date_str + '.zip' for date_str in ffill_dict[file]],
                                                             existing_output_files)

    else:
        files_for_stats = defaultdict(list)
        for date in get_dates_without_output(bucket_valid_files, existing_output_files):
            files_for_stats[date + '.zip'].append(date)

    logger.info(f'found {len([key for key in files_for_stats if len(files_for_stats[key])>0])} GTFS files valid for '
                'stats calculations in bucket')
    logger.debug(f'Files: { {key: value for key, value in files_for_stats.items() if len(files_for_stats[key])>0} }')
    return files_for_stats


def parse_date(file_name):
    """
Parse date from file name
    :param file_name: file name in YYYY-mm-dd.zip format
    :type file_name: str
    :return: date object and date string
    :rtype: tuple
    """
    date_str = file_name.split('.')[0]
    date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    return date, date_str


def get_gtfs_file(file, gtfs_folder, bucket, logger, force=False):
    """

    :param file: gtfs file name (currently only YYYY-mm-dd.zip)
    :type file: str
    :param gtfs_folder: local path containing GTFS feeds
    :type gtfs_folder: str
    :param bucket: s3 boto bucket object
    :type bucket: boto3.resources.factory.s3.Bucket
    :param logger: logger to write to
    :type logger: logging.Logger
    :param force: force download or not
    :type force: bool
    :return: whether file was downloaded or not
    :rtype: bool
    """
    if not force and os.path.exists(gtfs_folder + file):
        logger.info(f'found file "{file}" in local folder "{gtfs_folder}"')
        downloaded = False
    else:
        logger.info(f'starting file download with retries (key="{file}", local path="{gtfs_folder+file}")')
        s3_download(bucket, file, gtfs_folder + file, report=logger.error)
        logger.debug(f'finished file download (key="{file}", local path="{gtfs_folder+file}")')
        downloaded = True
    # TODO: log file size
    return downloaded


def handle_gtfs_date(date_str, file, bucket, output_folder=OUTPUT_DIR,
                     gtfs_folder=GTFS_FEEDS_PATH, logger=None):
    """
Handle a single date for a single GTFS file. Download if necessary compute and save stats files (currently trip_stats
and route_stats).
    :param date_str: %Y-%m-%d
    :type date_str: str
    :param file: gtfs file name (currently only YYYY-mm-dd.zip)
    :type file: str
    :param bucket: s3 boto bucket object
    :type bucket: boto3.resources.factory.s3.Bucket
    :param output_folder: local path to write output files to
    :type output_folder: str
    :param gtfs_folder: local path containing GTFS feeds
    :type gtfs_folder: str
    :param logger: logger to write to
    :type logger: logging.Logger
    """
    date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()

    downloaded = False

    trip_stats_output_path = output_folder + date_str + '_trip_stats.pkl.gz'
    if os.path.exists(trip_stats_output_path):
        logger.info(f'found trip stats result DF gzipped pickle "{trip_stats_output_path}"')
        ts = pd.read_pickle(trip_stats_output_path, compression='gzip')
    else:
        downloaded = get_gtfs_file(file, gtfs_folder, bucket, logger)

        if WRITE_FILTERED_FEED:
            filtered_out_path = FILTERED_FEEDS_PATH + date_str + '.zip'
            logger.info(f'writing filtered gtfs feed for file "{gtfs_folder+file}" with date "{date}" in path '
                        f'{filtered_out_path}')
            gu.write_filtered_feed_by_date(gtfs_folder + file, date, filtered_out_path)
            logger.info(f'reading filtered feed for file from path {filtered_out_path}')
            feed = ptg_feed(filtered_out_path)

        else:
            logger.info(f'creating daily partridge feed for file "{gtfs_folder+file}" with date "{date}"')
            try:
                feed = gu.get_partridge_feed_by_date(gtfs_folder + file, date)
            except BadZipFile:
                logger.error('Bad local zip file', exc_info=True)
                downloaded = get_gtfs_file(file, gtfs_folder, bucket, logger, force=True)
                feed = gu.get_partridge_feed_by_date(gtfs_folder + file, date)

        logger.debug(f'finished creating daily partridge feed for file "{gtfs_folder+file}" with date "{date}"')

        # TODO: add changing zones from archive
        logger.info(f'creating zones DF from "{LOCAL_TARIFF_PATH}"')
        zones = gu.get_zones_df(LOCAL_TARIFF_PATH)

        logger.info(
            f'starting compute_trip_stats_partridge for file "{gtfs_folder+file}" with date "{date}" and zones '
            f'"{LOCAL_TARIFF_PATH}"')
        ts = compute_trip_stats_partridge(feed, zones)
        logger.debug(
            f'finished compute_trip_stats_partridge for file "{gtfs_folder+file}" with date "{date}" and zones '
            f'"{LOCAL_TARIFF_PATH}"')
        # TODO: log this
        ts['date'] = date_str
        ts['date'] = pd.Categorical(ts.date)

        logger.info(f'saving trip stats result DF to gzipped pickle "{trip_stats_output_path}"')
        ts.to_pickle(trip_stats_output_path, compression='gzip')

    # TODO: log more stats
    logger.debug(
        f'ts.shape={ts.shape}, dc_trip_id={ts.trip_id.nunique()}, dc_route_id={ts.route_id.nunique()}, '
        f'num_start_zones={ts.start_zone.nunique()}, num_agency={ts.agency_name.nunique()}')

    logger.info(f'starting compute_route_stats_base_partridge from trip stats result')
    rs = compute_route_stats_base_partridge(ts)
    logger.debug(f'finished compute_route_stats_base_partridge from trip stats result')
    # TODO: log this
    rs['date'] = date_str
    rs['date'] = pd.Categorical(rs.date)

    # TODO: log more stats
    logger.debug(
        f'rs.shape={rs.shape}, num_trips_sum={rs.num_trips.sum()}, dc_route_id={rs.route_id.nunique()}, '
        f'num_start_zones={rs.start_zone.nunique()}, num_agency={rs.agency_name.nunique()}')

    route_stats_output_path = output_folder + date_str + '_route_stats.pkl.gz'
    logger.info(f'saving route stats result DF to gzipped pickle "{route_stats_output_path}"')
    rs.to_pickle(route_stats_output_path, compression='gzip')

    return downloaded


def handle_gtfs_file(file, bucket, stats_dates, output_folder=OUTPUT_DIR,
                     gtfs_folder=GTFS_FEEDS_PATH, delete_downloaded_gtfs_zips=False,
                     logger=None):
    """
Handle a single GTFS file. Download if necessary compute and save stats files (currently trip_stats and route_stats).
    :param file: gtfs file name (currently only YYYY-mm-dd.zip)
    :type file: str
    :param bucket: s3 boto bucket object
    :type bucket: boto3.resources.factory.s3.Bucket
    :param output_folder: local path to write output files to
    :type output_folder: str
    :param gtfs_folder: local path containing GTFS feeds
    :type gtfs_folder: str
    :param delete_downloaded_gtfs_zips: whether to delete GTFS feed files that have been downloaded by the function.
    :type delete_downloaded_gtfs_zips: bool
    :param logger: logger to write to
    :type logger: logging.Logger
    """

    downloaded = False
    with tqdm(stats_dates, postfix='initializing', unit='date', desc='dates', leave=False) as t:
        for date_str in t:
            t.set_postfix_str(date_str)
            downloaded = handle_gtfs_date(date_str, file, bucket, output_folder=output_folder,
                                          gtfs_folder=gtfs_folder, logger=logger)

    if delete_downloaded_gtfs_zips and downloaded:
        logger.info(f'deleting gtfs zip file "{gtfs_folder+file}"')
        os.remove(gtfs_folder + file)
    else:
        logger.debug(f'keeping gtfs zip file "{gtfs_folder+file}"')


def batch_stats_s3(bucket_name=BUCKET_NAME, output_folder=OUTPUT_DIR,
                   gtfs_folder=GTFS_FEEDS_PATH, delete_downloaded_gtfs_zips=False,
                   forward_fill=FORWARD_FILL, logger=None):
    """
Create daily trip_stats and route_stats DataFrame pickles, based on the files in an S3 bucket and
their dates - `YYYY-mm-dd.zip`.
Will look for downloaded GTFS feeds with matching names in given gtfs_folder.
    :param bucket_name: name of s3 bucket with GTFS feeds
    :type bucket_name: str
    :param output_folder: local path to write output files to
    :type output_folder: str
    :param gtfs_folder: local path containing GTFS feeds
    :type gtfs_folder: str
    :param delete_downloaded_gtfs_zips: whether to delete GTFS feed files that have been downloaded by the function.
    :type delete_downloaded_gtfs_zips: bool
    :param forward_fill: flag for performing forward fill for missing dates using existing files
    :type forward_fill: bool
    :param logger: logger to write to
    :type logger: logging.Logger
    """
    try:
        existing_output_files = []
        if os.path.exists(output_folder):
            existing_output_files = _get_existing_output_files(output_folder)
            logger.info(f'found {len(existing_output_files)} output files in output folder {output_folder}')
        else:
            logger.info(f'creating output folder {output_folder}')
            os.makedirs(output_folder)

        s3 = boto3.resource('s3')
        bucket = s3.Bucket(bucket_name)

        logger.info(f'connected to S3 bucket {bucket_name}')

        bucket_objects = bucket.objects.all()
        logger.info(f'number of objects in bucket: {sum(1 for _ in bucket_objects)}')

        if forward_fill:
            file_dates_dict = get_valid_file_dates_dict(bucket_objects, existing_output_files, logger,
                                                        forward_fill=True)
        else:
            file_dates_dict = get_valid_file_dates_dict(bucket_objects, existing_output_files, logger,
                                                        forward_fill=False)

        non_empty_file_dates = {key: value for key, value in file_dates_dict.items() if len(file_dates_dict[key]) > 0}
        with tqdm(non_empty_file_dates, postfix='initializing', unit='file', desc='files') as t:
            for file in t:
                t.set_postfix_str(file)
                handle_gtfs_file(file, bucket, output_folder=output_folder,
                                 gtfs_folder=gtfs_folder, delete_downloaded_gtfs_zips=delete_downloaded_gtfs_zips,
                                 stats_dates=file_dates_dict[file], logger=logger)

        logger.info(f'starting synchronous gtfs file download and stats computation from s3 bucket {bucket_name}')

        logger.info(f'finished synchronous gtfs file download and stats computation from s3 bucket {bucket_name}')
    except:
        logger.error('Failed', exc_info=True)


def get_logger():
    """
Returns a logger object. Writes all (up to DEBUG) to a file in the configured `LOG_FOLDER`. Errors are also written to
stdout.
TODO: use logging conf file
TODO: use __name__ and call this from each function
TODO: decorate
    :return: logger object
    :rtype: logging.Logger
    """
    # create logger with 'gtfs_stats'
    logger = logging.getLogger('gtfs_stats')
    logger.setLevel(logging.DEBUG)
    # create file handler which logs even debug messages
    fh = logging.FileHandler(LOG_FOLDER + f'gtfs_stats_{datetime.datetime.now().strftime("%Y%m%d%H%M%S")}.log')
    fh.setLevel(logging.DEBUG)
    # create console handler with a higher log level
    ch = logging.StreamHandler()
    ch.setLevel(logging.ERROR)
    # create formatter and add it to the handlers
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    # add the handlers to the logger
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def main():
    logger = get_logger()
    logger.info(f'starting batch_stats_s3 with default config')
    batch_stats_s3(delete_downloaded_gtfs_zips=DELETE_DOWNLOADED_GTFS_ZIPS, logger=logger)


if __name__ == '__main__':
    if PROFILE:
        import cProfile

        cProfile.run('main()', filename=PROFILE_PATH)
    else:
        main()

# ## What's next
# 
# TODO List
# 
# 1. add a function for handling today's file only (download from ftp)
# 1. remove zone and extra route details from trip_stats
#   1. add them by merging to route_stats
# 1. separate to modules - run, conf, stats, utils...
# 1. logging - 
#   1. logging config and call in every function
#   1. BUG: logger not logging retries
#   1. log __name__ with decorator
#   1. add ids to every record - process, file
# 1. run older files with older tariff file
# 1. write tests
# 1. add split_directions
# 1. add time between stops - max, min, mean (using delta)
# 1. add day and night headways and num_trips (maybe noon also)
# 1. create a function for reading all the pickles and 
#   make necessary conversions (make it a timeseries good for pandas)
#   1. times to proper datetimes, date to time period
#   1. bools to bools
#   1. add day of week
# 1. insert to sql
# 1. mean_headway doesn't mean much when num_trips low (maybe num_trips cutoffs will be enough)
