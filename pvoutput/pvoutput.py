"""PVOutput.org utils

## How to setup this notebook

"""
from io import StringIO
import sys
import os
import time
import logging
from datetime import datetime, timedelta
from urllib3.util.retry import Retry
import requests
from requests.adapters import HTTPAdapter
import numpy as np
import pandas as pd
from typing import Dict, Union

SECONDS_PER_DAY = 60 * 60 * 24
ONE_DAY = timedelta(days=1)
PV_OUTPUT_DATE_FORMAT = "%Y%m%d"


def get_logger(filename='/home/jack/data/pvoutput.org/logs/UK_PV_timeseries.log',
               mode='a',
               level=logging.DEBUG,
               stream_handler=False):
    logger = logging.getLogger(__name__)
    logger.setLevel(level)
    logger.handlers = [logging.FileHandler(filename=filename, mode=mode)]
    if stream_handler:
        logger.handlers.append(logging.StreamHandler(sys.stdout))
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    for handler in logger.handlers:
        handler.setFormatter(formatter)

    # Attach urllib3's logger to our logger.
    urllib3_log = logging.getLogger("urllib3")
    urllib3_log.parent = logger
    urllib3_log.propagate = True

    return logger


_logger = get_logger()


class BadStatusCode(Exception):
    def __init__(self, response: requests.Response, message: str = ''):
        self.response = response
        super(BadStatusCode, self).__init__(message)

    def __str__(self) -> str:
        string = super(BadStatusCode, self).__str__()
        string += "Status code: {}\n".format(self.response.status_code)
        string += "Response content: {}\n".format(self.response.content)
        string += "Response headers: {}".format(self.response.headers)
        return string


class NoStatusFound(BadStatusCode):
    pass


class RateLimitExceeded(BadStatusCode):
    def __init__(self, *args, **kwargs):
        super(RateLimitExceeded, self).__init__(*args, **kwargs)
        self._set_params()

    def _set_params(self):
        self.utc_now = datetime.utcnow()
        self.rate_limit_reset_datetime = datetime.utcfromtimestamp(
            int(self.response.headers['X-Rate-Limit-Reset']))
        self.timedelta_to_wait = self.rate_limit_reset_datetime - self.utc_now
        self.timedelta_to_wait += timedelta(minutes=3)  # Just for safety
        self.secs_to_wait = self.timedelta_to_wait.total_seconds()

    def __str__(self) -> str:
        return 'Rate limit exceeded!'

    def wait_message(self) -> str:
        retry_time_utc = self.utc_now + self.timedelta_to_wait
        return '{}  Waiting {:.0f} seconds.  Will retry at {} UTC.'.format(
            self, self.secs_to_wait,
            retry_time_utc.strftime('%Y-%m-%d %H:%M:%S'))


def _get_session_with_retry() -> requests.Session:
    session = requests.Session()
    max_retry_counts = dict(
        connect=720,  # How many connection-related errors to retry on.
                      # Set high because sometimes the network goes down for a
                      # few hours at a time.
                      # 720 x Retry.MAX_BACKOFF (120 s) = 86,400 s = 24 hrs
        read=5,  # How many times to retry on read errors.
        status=5  # How many times to retry on bad status codes.
    )
    retries = Retry(
        total=max(max_retry_counts.values()),
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        **max_retry_counts
    )
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


def _get_api_reponse(service: str, api_params: Dict) -> requests.Response:
    """
    Args:
        service: string, e.g. 'search', 'getstatus'
        api_params: dict
    """
    # Create request headers
    headers = {
        'X-Pvoutput-Apikey': os.environ['PVOUTPUT_APIKEY'],
        'X-Pvoutput-SystemId': os.environ['PVOUTPUT_SYSTEMID'],
        'X-Rate-Limit': '1'}

    # Create request URL
    api_base_url = 'https://pvoutput.org/service/r2/{}.jsp'.format(service)
    api_params_str = '&'.join(
        ['{}={}'.format(key, value) for key, value in api_params.items()])
    api_url = '{}?{}'.format(api_base_url, api_params_str)

    session = _get_session_with_retry()
    response = session.get(api_url, headers=headers)

    _logger.debug(
        'response: status_code=%d; headers=%s',
        response.status_code, response.headers)
    return response


def _process_api_response(response: requests.Response) -> str:
    """Turns an API response into text.

    Args:
        response: from _get_api_reponse()

    Returns:
        content of the response.

    Raises:
        UnicodeDecodeError
        NoStatusFound
        RateLimitExceeded
    """
    try:
        content = response.content.decode('latin1').strip()
    except UnicodeDecodeError as e:
        msg = "Error decoding this string: {}\n{}".format(response.content, e)
        _logger.exception(msg)
        raise

    if response.status_code == 400:
        raise NoStatusFound(response=response)

    # Did we overshoot our quota?
    rate_limit_remaining = int(response.headers['X-Rate-Limit-Remaining'])
    _logger.debug('Remaining API requests: %d', rate_limit_remaining)
    if response.status_code == 403 and rate_limit_remaining <= 0:
        raise RateLimitExceeded(response=response)

    response.raise_for_status()

    # If we get to here then the content is valid :)
    return content


def pv_output_api_query(service: str,
                        api_params: Dict,
                        wait_if_rate_limit_exceeded: bool = True
                        ) -> str:
    """Send API request to PVOutput.org and return content text.

    Args:
        service: string, e.g. 'search' or 'getstatus'
        api_params: dict
        wait_if_rate_limit_exceeded: bool

    Raises:
        NoStatusFound
        RateLimitExceeded
    """
    try:
        response = _get_api_reponse(service, api_params)
    except Exception as e:
        _logger.exception(e)
        raise

    try:
        return _process_api_response(response)
    except RateLimitExceeded as e:
        if wait_if_rate_limit_exceeded:
            _logger.info(e.wait_message())
            time.sleep(e.secs_to_wait)
            return pv_output_api_query(
                service, api_params, wait_if_rate_limit_exceeded=False)
        else:
            raise


def pv_system_search(query: str, lat_lon: str, **kwargs) -> pd.DataFrame:
    """Send a search query to PVOutput.org.

    Some quirks of the PVOutput.org API:
        - The maximum number of results returned by PVOutput.org is 30.
            If the number of returned results is 30, then there is indication
            of whether there are exactly 30 search results, or if there
            are more than 30.  Also, there is no way to request additional
            'pages' of search results.
        - The maximum search radius is 25km

    Args:
        query: string, see https://pvoutput.org/help.html#search
            e.g. '5km'.
        lat_lon: string, e.g. '52.0668589,-1.3484038'

    Returns:
        pd.DataFrame, one row per search results.  Index is PV system ID (int).
            Columns:
                system_name,
                system_size_watts,
                postcode,  # including the country
                orientation,
                num_outputs,
                last_output,
                panel,
                inverter,
                distance_km,
                latitude,
                longitude
    """

    pv_systems_text = pv_output_api_query(
        service='search',
        api_params={
            'q': query,
            'll': lat_lon,
            'country': 1  # Country flag, whether or not to return country
                          # with the postcode.
        }, **kwargs)

    pv_systems = pd.read_csv(
        StringIO(pv_systems_text),
        names=[
            'system_name',
            'system_size_watts',
            'postcode',
            'orientation',
            'num_outputs',
            'last_output',
            'system_id',
            'panel',
            'inverter',
            'distance_km',
            'latitude',
            'longitude'],
        index_col='system_id')

    return pv_systems


def date_to_pvoutput_str(date: Union[str, datetime]) -> str:
    """Convert datetime to date string for PVOutput.org in YYYYMMDD format."""
    if isinstance(date, str):
        return date
    else:
        return date.strftime(PV_OUTPUT_DATE_FORMAT)


def _check_date(date: str):
    """Check that date string conforms to YYYYMMDD format,
    and that the date isn't in the future.

    Raises:
        ValueError if the date is 'bad'.
    """
    dt = datetime.strptime(date, PV_OUTPUT_DATE_FORMAT)
    if dt > datetime.now():
        raise ValueError(
            'date should not be in the future.  Got {}.  Current date is {}.'
            .format(date, datetime.now()))


def get_pv_system_status(pv_system_id: int,
                         date: str,
                         **kwargs
                         ) -> pd.DataFrame:
    """Get PV system status (e.g. instantaneous power generation) for one day.

    Args:
        pv_system_id: int
        date: str, YYYYMMDD

    Returns:
        pd.DataFrame:
            index: datetime (DatetimeIndex, localtime of the PV system)
            columns:  (all np.float64):
                energy_generation_watt_hours,
                energy_efficiency_kWh_per_kW,
                inst_power_watt,
                average_power_watt,
                normalised_output,
                energy_consumption_watt_hours,
                power_consumption_watts,
                temperature_celsius,
                voltage
    """
    date = date_to_pvoutput_str(date)
    _check_date(date)

    pv_system_status_text = pv_output_api_query(
        service='getstatus',
        api_params={
            'd': date,  # date, YYYYMMDD.
            'h': 1,  # We want historical data.
            'limit': 288,  # API limit is 288 (num of 5-min periods per day).
            'ext': 0,  # Extended data; we don't want extended data.
            'sid1': pv_system_id  # SystemID.
        },
        **kwargs)

    columns = [
        'energy_generation_watt_hours',
        'energy_efficiency_kWh_per_kW',
        'inst_power_watt',
        'average_power_watt',
        'normalised_output',
        'energy_consumption_watt_hours',
        'power_consumption_watts',
        'temperature_celsius',
        'voltage']

    pv_system_status = pd.read_csv(
        StringIO(pv_system_status_text),
        lineterminator=';',
        names=['date', 'time'] + columns,
        parse_dates={'datetime': ['date', 'time']},
        index_col=['datetime'],
        dtype={col: np.float64 for col in columns}
    ).sort_index()

    return pv_system_status


def check_pv_system_status(pv_system_status: pd.DataFrame,
                           requested_date_str: str):
    """Checks the DataFrame returned by get_pv_system_status.

    Args:
        pv_system_status: DataFrame returned by get_pv_system_status
        requested_date_str: Date string in YYYYMMDD format.

    Raises:
        ValueError if the DataFrame is incorrect.
    """
    if not isinstance(pv_system_status, pd.DataFrame):
        raise ValueError('pv_system_status must be a dataframe')
    requested_date = datetime.strptime(requested_date_str, "%Y%m%d").date()
    if len(pv_system_status) > 0:
        index = pv_system_status.index
        for d in [index[0], index[-1]]:
            if not (requested_date <= d.date() <= requested_date + ONE_DAY):
                raise ValueError(
                      'A date in the index is outside the expected range.'
                      ' Date from index={}, requested_date={}'
                      .format(d, requested_date_str))


def get_pv_metadata(pv_system_id: int, **kwargs) -> pd.Series:
    """Calls PVOutput.org's 'getsystem' service to get metadata for a single PV
    system.

    Args:
        pv_system_id: int

    Returns:
        pd.Series.  Index is:
            system_name,
            system_id,
            system_size_watts,
            postcode,
            number_of_panels,
            panel_power_watts,
            panel_brand,
            num_inverters,
            inverter_power_watts,
            inverter_brand,
            orientation,
            array_tilt_degrees,
            shade,
            install_date,
            latitude,
            longitude,
            status_interval_minutes,
            number_of_panels_secondary,
            panel_power_watts_secondary,
            orientation_secondary,
            array_tilt_degrees_secondary
    """
    pv_metadata_text = pv_output_api_query(
        service='getsystem',
        api_params={
            'array2': 1,  # Provide data about secondary array, if present.
            'tariffs': 0,
            'teams': 0,
            'est': 0,
            'donations': 0,
            'sid1': pv_system_id,  # SystemID
            'ext': 0,  # Include extended data?
        }, **kwargs)

    pv_metadata = pd.read_csv(
        StringIO(pv_metadata_text),
        lineterminator=';',
        names=[
            'system_name',
            'system_size_watts',
            'postcode',
            'number_of_panels',
            'panel_power_watts',
            'panel_brand',
            'num_inverters',
            'inverter_power_watts',
            'inverter_brand',
            'orientation',
            'array_tilt_degrees',
            'shade',
            'install_date',
            'latitude',
            'longitude',
            'status_interval_minutes',
            'number_of_panels_secondary',
            'panel_power_watts_secondary',
            'orientation_secondary',
            'array_tilt_degrees_secondary'
        ],
        parse_dates=['install_date'],
        nrows=1
    ).squeeze()
    pv_metadata['system_id'] = pv_system_id
    pv_metadata.name = pv_system_id

    return pv_metadata


def get_pv_statistic(pv_system_id: int, **kwargs) -> pd.Series:
    """Calls PVOutput.org's 'getstatistic' service to get summary stats for a
    single PV system

    Args:
        pv_system_id: int

    Returns:
        pd.Series with index:
            energy_generated_Wh,
            energy_exported_Wh,
            average_generation_Wh,
            minimum_generation_Wh,
            maximum_generation_Wh,
            average_efficiency_kWh_per_kW,
            outputs,  # The total number of data outputs recorded by PVOutput
            actual_date_from,
            actual_date_to,
            record_efficiency_kWh_per_kW,
            record_efficiency_date,
            system_id
    """

    pv_metadata_text = pv_output_api_query(
        service='getstatistic',
        api_params={
            'c': 0,  # consumption and import
            'crdr': 0,  # credits / debits
            'sid1': pv_system_id,  # SystemID
        },
        **kwargs)

    pv_metadata = pd.read_csv(
        StringIO(pv_metadata_text),
        names=[
            'energy_generated_Wh',
            'energy_exported_Wh',
            'average_generation_Wh',
            'minimum_generation_Wh',
            'maximum_generation_Wh',
            'average_efficiency_kWh_per_kW',
            'outputs',
            'actual_date_from',
            'actual_date_to',
            'record_efficiency_kWh_per_kW',
            'record_efficiency_date'
        ],
        parse_dates=[
            'actual_date_from',
            'actual_date_to',
            'record_efficiency_date'
        ]
    ).squeeze()
    pv_metadata['system_id'] = pv_system_id
    pv_metadata.name = pv_system_id
    return pv_metadata