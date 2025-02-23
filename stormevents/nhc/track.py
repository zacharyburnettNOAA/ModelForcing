import gzip
import io
import logging
import pathlib
import re
import warnings
from datetime import datetime
from datetime import timedelta
from functools import partial
from os import PathLike
from typing import Any
from typing import Dict
from typing import List
from typing import Union
from urllib.error import URLError
from urllib.request import urlopen

import numpy
import pandas
import typepigeon
from pandas import DataFrame, Timedelta
from pyproj import Geod
from shapely import ops
from shapely.geometry import LineString
from shapely.geometry import MultiPolygon
from shapely.geometry import Polygon

from stormevents.nhc.atcf import ATCF_Advisory
from stormevents.nhc.atcf import ATCF_FileDeck
from stormevents.nhc.atcf import ATCF_Mode
from stormevents.nhc.atcf import atcf_url
from stormevents.nhc.atcf import EXTRA_ATCF_FIELDS
from stormevents.nhc.atcf import get_atcf_entry
from stormevents.nhc.atcf import read_atcf
from stormevents.nhc.storms import nhc_storms
from stormevents.nhc.const import (
    get_RMW_regression_coefs,
    RMW_bias_correction,
    RMWFillMethod,
)
from stormevents.utilities import subset_time_interval


class VortexTrack:
    """
    interface to an ATCF vortex track (i.e. HURDAT, best track, HWRF, etc.)
    """

    def __init__(
        self,
        storm: Union[str, PathLike, DataFrame, io.BytesIO],
        start_date: datetime = None,
        end_date: datetime = None,
        file_deck: ATCF_FileDeck = None,
        advisories: List[ATCF_Advisory] = None,
        forecast_time: datetime = None,
        rmw_fill: RMWFillMethod = RMWFillMethod.regression_penny_2023,
    ):
        """
        :param storm: storm ID, or storm name and year
        :param start_date: start date of track
        :param end_date: end date of track
        :param file_deck: ATCF file deck; one of `a`, `b`, `f`
        :param advisories: ATCF advisory type; one of ``BEST``, ``OFCL``, ``OFCP``, ``HMON``, ``CARQ``, ``HWRF``

        >>> VortexTrack('AL112017')
        VortexTrack('AL112017', Timestamp('2017-08-30 00:00:00'), Timestamp('2017-09-13 12:00:00'), <ATCF_FileDeck.BEST: 'b'>, <ATCF_Mode.HISTORICAL: 'ARCHIVE'>, [<ATCF_Advisory.BEST: 'BEST'>], None)

        >>> VortexTrack('AL112017', start_date='2017-09-04')
        VortexTrack('AL112017', Timestamp('2017-09-04 00:00:00'), Timestamp('2017-09-13 12:00:00'), <ATCF_FileDeck.BEST: 'b'>, <ATCF_Mode.HISTORICAL: 'ARCHIVE'>, [<ATCF_Advisory.BEST: 'BEST'>], None)

        >>> from datetime import timedelta
        >>> VortexTrack('AL112017', start_date=timedelta(days=2), end_date=timedelta(days=-1))
        VortexTrack('AL112017', Timestamp('2017-09-01 00:00:00'), Timestamp('2017-09-12 12:00:00'), <ATCF_FileDeck.BEST: 'b'>, <ATCF_Mode.HISTORICAL: 'ARCHIVE'>, [<ATCF_Advisory.BEST: 'BEST'>], None)

        >>> VortexTrack('AL112017', file_deck='a')
        VortexTrack('AL112017', Timestamp('2017-08-27 06:00:00'), Timestamp('2017-09-13 12:00:00'), <ATCF_FileDeck.ADVISORY: 'a'>, <ATCF_Mode.HISTORICAL: 'ARCHIVE'>, ['OFCL', 'OFCP', 'HMON', 'CARQ', 'HWRF'], None)
        """

        self.__unfiltered_data = None
        self.__filename = None

        self.__remote_atcf = None
        self.__nhc_code = None
        self.__name = None
        self.__start_date = None
        self.__end_date = None
        self.__file_deck = None
        self.__advisories = None
        self.__forecast_time = None

        self.__advisories_to_remove = []
        self.__invalid_storm_name = False
        self.__location_hash = None
        self.__linestrings = None
        self.__distances = None

        if isinstance(storm, DataFrame):
            self.__unfiltered_data = storm
        elif pathlib.Path(storm).exists():
            self.filename = storm
        elif isinstance(storm, str):
            try:
                self.nhc_code = storm
            except ValueError:
                raise
        else:
            raise FileNotFoundError(f'file not found "{storm}"')

        self.advisories = advisories
        self.file_deck = file_deck
        self.rmw_fill = rmw_fill

        self.__previous_configuration = self.__configuration

        # use start and end dates to mask dataframe here
        self.start_date = start_date
        self.end_date = end_date
        self.forecast_time = forecast_time

    @classmethod
    def from_storm_name(
        cls,
        name: str,
        year: int,
        start_date: datetime = None,
        end_date: datetime = None,
        file_deck: ATCF_FileDeck = None,
        advisories: List[ATCF_Advisory] = None,
        forecast_time: datetime = None,
        rmw_fill: RMWFillMethod = RMWFillMethod.regression_penny_2023,
    ) -> "VortexTrack":
        """
        :param name: storm name
        :param year: storm year
        :param start_date: start date of track
        :param end_date: end date of track
        :param file_deck: ATCF file deck; one of ``a``, ``b``, ``f``
        :param advisories: list of ATCF advisory types; valid choices are: ``BEST``, ``OFCL``, ``OFCP``, ``HMON``, ``CARQ``, `HWRF``

        >>> VortexTrack.from_storm_name('irma', 2017)
        VortexTrack('AL112017', Timestamp('2017-08-30 00:00:00'), Timestamp('2017-09-13 12:00:00'), <ATCF_FileDeck.BEST: 'b'>, [<ATCF_Advisory.BEST: 'BEST'>], None)
        """

        year = int(year)
        atcf_id = get_atcf_entry(storm_name=name, year=year).name

        return cls(
            storm=atcf_id,
            start_date=start_date,
            end_date=end_date,
            file_deck=file_deck,
            advisories=advisories,
            forecast_time=forecast_time,
            rmw_fill=rmw_fill,
        )

    @classmethod
    def from_file(
        cls,
        path: PathLike,
        start_date: datetime = None,
        end_date: datetime = None,
        file_deck: ATCF_FileDeck = None,
        advisories: List[ATCF_Advisory] = None,
        forecast_time: datetime = None,
        rmw_fill: RMWFillMethod = RMWFillMethod.regression_penny_2023,
    ) -> "VortexTrack":
        """
        :param path: file path to ATCF data
        :param start_date: start date of track
        :param end_date: end date of track
        :param file_deck: ATCF file deck; one of ``a``, ``b``, ``f``
        :param advisories: list of ATCF advisory types; valid choices are: ``BEST``, ``OFCL``, ``OFCP``, ``HMON``, ``CARQ``, `HWRF``

        >>> VortexTrack.from_file('tests/data/input/test_vortex_track_from_file/AL062018.dat')
        VortexTrack('AL062018', Timestamp('2018-08-30 06:00:00'), Timestamp('2018-09-18 12:00:00'), None, <ATCF_Mode.HISTORICAL: 'ARCHIVE'>, ['BEST', 'OFCL', 'OFCP', 'HMON', 'CARQ', 'HWRF'], PosixPath('/home/zrb/Projects/StormEvents/tests/data/input/test_vortex_track_from_file/AL062018.dat'))
        >>> VortexTrack.from_file('tests/data/input/test_vortex_track_from_file/irma2017_fort.22')
        VortexTrack('AL112017', Timestamp('2017-09-05 00:00:00'), Timestamp('2017-09-12 00:00:00'), None, <ATCF_Mode.HISTORICAL: 'ARCHIVE'>, ['BEST', 'OFCL', 'OFCP', 'HMON', 'CARQ', 'HWRF'], PosixPath('/home/zrb/Projects/StormEvents/tests/data/input/test_vortex_track_from_file/irma2017_fort.22'))
        """

        if file_deck is None and advisories is None:
            warnings.warn(
                "It is recommended to specify the file_deck and/or advisories when reading from file"
            )

        try:
            path = pathlib.Path(path)
        except:
            pass

        return cls(
            storm=path,
            start_date=start_date,
            end_date=end_date,
            file_deck=file_deck,
            advisories=advisories,
            forecast_time=forecast_time,
            rmw_fill=rmw_fill,
        )

    @property
    def name(self) -> str:
        """
        :return: NHC storm name

        >>> track = VortexTrack('AL112017')
        >>> track.name
        'IRMA'
        """

        if self.__name is None:
            # get the most frequently-used storm name in the data
            names = self.data["name"].value_counts()
            if len(names) > 0:
                name = names.index[0]
            else:
                name = ""

            if name.strip() == "":
                storms = nhc_storms(year=self.year)
                if self.nhc_code.upper() in storms.index:
                    storm = storms.loc[self.nhc_code.upper()]
                    name = storm["name"].lower()

            self.__name = name

        return self.__name

    @property
    def basin(self) -> str:
        """
        :return: basin of track

        >>> track = VortexTrack('AL112017')
        >>> track.basin
        'AL'
        """

        return self.data["basin"].iloc[0]

    @property
    def storm_number(self) -> str:
        """
        :return: ordinal number of storm within the basin and year

        >>> track = VortexTrack('AL112017')
        >>> track.storm_number
        11
        """

        return self.data["storm_number"].iloc[0]

    @property
    def year(self) -> int:
        """
        :return: year of storm

        >>> track = VortexTrack('AL112017')
        >>> track.year
        2017
        """

        return self.data["datetime"].iloc[0].year

    @property
    def nhc_code(self) -> str:
        """
        :return: storm NHC code (i.e. ``AL062018``)

        >>> track = VortexTrack('AL112017')
        >>> track.nhc_code
        'AL112017'
        """

        if self.__nhc_code is None and not self.__invalid_storm_name:
            if self.__unfiltered_data is not None:
                nhc_code = (
                    f'{self.__unfiltered_data["basin"].iloc[-1]}'
                    f'{self.__unfiltered_data["storm_number"].iloc[-1]}'
                    f'{self.__unfiltered_data["datetime"].iloc[-1].year}'
                )
                try:
                    self.nhc_code = nhc_code
                except ValueError:
                    try:
                        nhc_code = get_atcf_entry(
                            storm_name=self.__unfiltered_data["name"].tolist()[-1],
                            year=self.__unfiltered_data["datetime"].tolist()[-1].year,
                        ).name
                        self.nhc_code = nhc_code
                    except ValueError:
                        self.__invalid_storm_name = True
        return self.__nhc_code

    @nhc_code.setter
    def nhc_code(self, nhc_code: str):
        if nhc_code is not None:
            # check if name+year was given instead of basin+number+year
            digits = sum([1 for character in nhc_code if character.isdigit()])

            if digits == 4:
                atcf_nhc_code = get_atcf_entry(
                    storm_name=nhc_code[:-4], year=int(nhc_code[-4:])
                ).name
                if atcf_nhc_code is None:
                    raise ValueError(f"No storm with id: {nhc_code}")
                nhc_code = atcf_nhc_code
        self.__nhc_code = nhc_code

    @property
    def start_date(self) -> pandas.Timestamp:
        """
        :return: start time of current track

        >>> track = VortexTrack('AL112017')
        >>> track.start_date
        Timestamp('2017-08-30 00:00:00')
        >>> track.start_date = '2017-09-04'
        >>> track.start_date
        Timestamp('2017-09-04 00:00:00')
        >>> from datetime import timedelta
        >>> track.start_date = timedelta(days=1)
        >>> track.start_date
        Timestamp('2017-08-31 00:00:00')
        >>> track.start_date = timedelta(days=-2)
        >>> track.start_date
        Timestamp('2017-09-11 12:00:00')
        """

        return self.__start_date

    @start_date.setter
    def start_date(self, start_date: datetime):
        data_start = self.unfiltered_data["datetime"].iloc[0]

        if start_date is None:
            start_date = data_start
        else:
            # interpret timedelta as a temporal movement around start / end
            data_end = self.unfiltered_data["datetime"].iloc[-1]
            start_date, _ = subset_time_interval(
                start=data_start,
                end=data_end,
                subset_start=start_date,
            )
            if not isinstance(start_date, pandas.Timestamp):
                start_date = pandas.to_datetime(start_date)

        self.__start_date = start_date

    @property
    def end_date(self) -> pandas.Timestamp:
        """
        :return: end time of current track

        >>> track = VortexTrack('AL112017')
        >>> track.end_date
        Timestamp('2017-09-13 12:00:00')
        >>> track.end_date = '2017-09-10'
        >>> track.end_date
        Timestamp('2017-09-10 00:00:00')
        >>> from datetime import timedelta
        >>> track.end_date = timedelta(days=-1)
        >>> track.end_date
        Timestamp('2017-09-12 12:00:00')
        >>> track.end_date = timedelta(days=2)
        >>> track.end_date
        Timestamp('2017-09-01 00:00:00')
        """

        return self.__end_date

    @end_date.setter
    def end_date(self, end_date: datetime):
        data_end = self.unfiltered_data["datetime"].iloc[-1]

        if end_date is None:
            end_date = data_end
        else:
            # interpret timedelta as a temporal movement around start / end
            data_start = self.unfiltered_data["datetime"].iloc[0]
            _, end_date = subset_time_interval(
                start=data_start,
                end=data_end,
                subset_end=end_date,
            )
            if not isinstance(end_date, pandas.Timestamp):
                end_date = pandas.to_datetime(end_date)

        self.__end_date = end_date

    @property
    def forecast_time(self) -> pandas.Timestamp:
        """
        :return: forecast time of forecast track
        """

        return self.__forecast_time

    @forecast_time.setter
    def forecast_time(self, forecast_time: datetime):
        if forecast_time is not None:
            forecast_time = pandas.to_datetime(forecast_time)

            if self.file_deck != ATCF_FileDeck.ADVISORY:
                raise ValueError("Forecast time only applies to forecast advisories")

            # NOTE: Cannot cleanly check forecast_time against
            # start and end date, since the first is on `track_start_time`
            # and the latter two on `datetime`
            # if not (self.start_date < forecast_time < self.end_date):
            #     raise ValueError(
            #         "The specified forecast time is outside available date range"
            #     )

        self.__forecast_time = forecast_time

    @property
    def file_deck(self) -> ATCF_FileDeck:
        """
        :return: ATCF file deck; one of ``a``, ``b``, ``f``
        """

        return self.__file_deck

    @file_deck.setter
    def file_deck(self, file_deck: ATCF_FileDeck):
        if file_deck is None and self.filename is None:
            if self.advisories is not None or len(self.advisories) > 0:
                if ATCF_Advisory.BEST in typepigeon.convert_value(
                    self.advisories, [ATCF_Advisory]
                ):
                    file_deck = ATCF_FileDeck.BEST
                else:
                    file_deck = ATCF_FileDeck.ADVISORY
            else:
                file_deck = ATCF_FileDeck.BEST
        elif not isinstance(file_deck, ATCF_FileDeck):
            file_deck = typepigeon.convert_value(file_deck, ATCF_FileDeck)
        self.__file_deck = file_deck

    @property
    def advisories(self) -> List[ATCF_Advisory]:
        """
        :return: ATCF advisory types; one of ``BEST``, ``OFCL``, ``OFCP``, ``HMON``, ``CARQ``, ``HWRF``
        """

        if self.file_deck == ATCF_FileDeck.BEST:
            self.__advisories = [ATCF_Advisory.BEST]

        return self.__advisories

    @advisories.setter
    def advisories(self, advisories: List[ATCF_Advisory]):
        # e.g. `BEST`, `OFCL`, `HWRF`, etc.
        if advisories is None:
            advisories = self.__valid_advisories
        else:
            advisories = typepigeon.convert_value(advisories, [str])
            advisories = [advisory.upper() for advisory in advisories]
        self.__advisories = advisories

    @property
    def __valid_advisories(self) -> List[ATCF_Advisory]:
        if self.file_deck is None:
            valid_advisories = [advisory.value for advisory in ATCF_Advisory]
        elif self.file_deck == ATCF_FileDeck.ADVISORY:
            # see ftp://ftp.nhc.noaa.gov/atcf/docs/nhc_techlist.dat
            # there are more but they may not have enough columns
            valid_advisories = [
                entry.value for entry in ATCF_Advisory if entry != ATCF_Advisory.BEST
            ]
        elif self.file_deck == ATCF_FileDeck.BEST:
            valid_advisories = [ATCF_Advisory.BEST.value]
        elif self.file_deck == ATCF_FileDeck.FIXED:
            valid_advisories = [entry.value for entry in ATCF_Advisory]
        else:
            raise NotImplementedError(
                f"file deck {self.file_deck.value} not implemented"
            )

        return valid_advisories

    @property
    def filename(self) -> pathlib.Path:
        """
        :return: file path to read file (optional)
        """

        return self.__filename

    @filename.setter
    def filename(self, filename: PathLike):
        if filename is not None and not isinstance(filename, pathlib.Path):
            filename = pathlib.Path(filename)
        self.__filename = filename

    @property
    def rmw_fill(self) -> RMWFillMethod:
        """
        :return: file path to read file (optional)
        """

        return self.__rmw_fill

    @rmw_fill.setter
    def rmw_fill(self, rmw_fill: RMWFillMethod):
        if rmw_fill is None or not isinstance(rmw_fill, RMWFillMethod):
            rmw_fill = RMWFillMethod.none

        self.__rmw_fill = rmw_fill

    @property
    def data(self) -> DataFrame:
        """
        :return: track data for the given parameters as a data frame

        >>> track = VortexTrack('AL112017')
        >>> track.data
            basin storm_number            datetime advisory_number  ... isowave_radius_for_SWQ extra_values                    geometry  track_start_time
        0      AL           11 2017-08-30 00:00:00                  ...                    NaN         <NA>  POINT (-26.90000 16.10000)        2017-08-30
        1      AL           11 2017-08-30 06:00:00                  ...                    NaN         <NA>  POINT (-28.30000 16.20000)        2017-08-30
        2      AL           11 2017-08-30 12:00:00                  ...                    NaN         <NA>  POINT (-29.70000 16.30000)        2017-08-30
        3      AL           11 2017-08-30 18:00:00                  ...                    NaN         <NA>  POINT (-30.80000 16.30000)        2017-08-30
        4      AL           11 2017-08-30 18:00:00                  ...                    NaN         <NA>  POINT (-30.80000 16.30000)        2017-08-30
        ..    ...          ...                 ...             ...  ...                    ...          ...                         ...               ...
        168    AL           11 2017-09-12 12:00:00                  ...                    NaN         <NA>  POINT (-86.90000 33.80000)        2017-08-30
        169    AL           11 2017-09-12 18:00:00                  ...                    NaN         <NA>  POINT (-88.10000 34.80000)        2017-08-30
        170    AL           11 2017-09-13 00:00:00                  ...                    NaN         <NA>  POINT (-88.90000 35.60000)        2017-08-30
        171    AL           11 2017-09-13 06:00:00                  ...                    NaN         <NA>  POINT (-89.50000 36.20000)        2017-08-30
        172    AL           11 2017-09-13 12:00:00                  ...                    NaN         <NA>  POINT (-90.10000 36.80000)        2017-08-30
        [173 rows x 38 columns]

        >>> track = VortexTrack('AL112017', file_deck='a')
        >>> track.data
              basin storm_number            datetime advisory_number  ... isowave_radius_for_SWQ extra_values                    geometry    track_start_time
        0        AL           11 2017-08-27 06:00:00              01  ...                    NaN         <NA>  POINT (-17.40000 11.70000) 2017-08-28 06:00:00
        1        AL           11 2017-08-27 12:00:00              01  ...                    NaN         <NA>  POINT (-17.90000 11.80000) 2017-08-28 06:00:00
        2        AL           11 2017-08-27 18:00:00              01  ...                    NaN         <NA>  POINT (-18.40000 11.90000) 2017-08-28 06:00:00
        3        AL           11 2017-08-28 00:00:00              01  ...                    NaN         <NA>  POINT (-19.00000 12.00000) 2017-08-28 06:00:00
        4        AL           11 2017-08-28 06:00:00              01  ...                    NaN         <NA>  POINT (-19.50000 12.00000) 2017-08-28 06:00:00
        ...     ...          ...                 ...             ...  ...                    ...          ...                         ...                 ...
        10739    AL           11 2017-09-12 00:00:00              03  ...                    NaN         <NA>  POINT (-84.40000 31.90000) 2017-09-12 00:00:00
        10740    AL           11 2017-09-12 03:00:00              03  ...                    NaN         <NA>  POINT (-84.90000 32.40000) 2017-09-12 00:00:00
        10741    AL           11 2017-09-12 12:00:00              03  ...                    NaN         <NA>  POINT (-86.40000 33.80000) 2017-09-12 00:00:00
        10742    AL           11 2017-09-13 00:00:00              03  ...                    NaN         <NA>  POINT (-88.20000 35.20000) 2017-09-12 00:00:00
        10743    AL           11 2017-09-13 12:00:00              03  ...                    NaN         <NA>  POINT (-88.60000 36.40000) 2017-09-12 00:00:00
        [10434 rows x 38 columns]
        """

        if self.forecast_time is not None:
            return self.unfiltered_data.loc[
                (self.unfiltered_data["track_start_time"] == self.forecast_time)
                & (self.unfiltered_data["datetime"] >= self.start_date)
                & (self.unfiltered_data["datetime"] <= self.end_date)
            ]
        return self.unfiltered_data.loc[
            (self.unfiltered_data["datetime"] >= self.start_date)
            & (self.unfiltered_data["datetime"] <= self.end_date)
        ]

    def to_file(
        self, path: PathLike, advisory: ATCF_Advisory = None, overwrite: bool = False
    ):
        """
        write track to file path

        :param path: output file path
        :param advisory: advisory type to write
        :param overwrite: overwrite existing file
        """

        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)

        if overwrite or not path.exists():
            if path.suffix == ".dat":
                data = self.atcf(advisory=advisory)
                data.to_csv(path, index=False, header=False)
            elif path.suffix == ".22":
                data = self.fort_22(advisory=advisory)
                data.to_csv(path, index=False, header=False)
            else:
                raise NotImplementedError(f"writing to `*{path.suffix}` not supported")
        else:
            logging.warning(f'skipping existing file "{path}"')

    def atcf(self, advisory: ATCF_Advisory = None) -> DataFrame:
        """
        https://www.nrlmry.navy.mil/atcf_web/docs/database/new/abrdeck.html

        BASIN,CY,YYYYMMDDHH,TECHNUM/MIN,TECH,TAU,LatN/S,LonE/W,VMAX,MSLP,TY,RAD,WINDCODE,RAD1,RAD2,RAD3,RAD4,RADP,RRP,MRD,GUSTS,EYE,SUBREGION,MAXSEAS,INITIALS,DIR,SPEED,STORMNAME,DEPTH,SEAS,SEASCODE,SEAS1,SEAS2,SEAS3,SEAS4,USERDEFINED,userdata

        :param advisory: advisory type
        :return: dataframe of CSV lines in ATCF format
        """

        atcf = self.data.copy(deep=True)
        atcf.loc[atcf["advisory"] != "BEST", "datetime"] = atcf.loc[
            atcf["advisory"] != "BEST", "track_start_time"
        ]
        atcf.drop(columns=["geometry", "track_start_time"], inplace=True)

        if advisory is not None:
            if isinstance(advisory, ATCF_Advisory):
                advisory = advisory.value
            atcf = atcf[atcf["advisory"] == advisory]

        atcf.loc[:, ["longitude", "latitude"]] = (
            atcf.loc[:, ["longitude", "latitude"]] * 10
        )

        float_columns = atcf.select_dtypes(include=["float"]).columns
        integer_na_value = -99999
        for column in float_columns:
            atcf.loc[pandas.isna(atcf[column]), column] = integer_na_value
            # Due to update in pandas 2
            atcf.loc[:, column] = atcf.loc[:, column].round(0)
        atcf = atcf.astype({col: int for col in float_columns})

        atcf["basin"] = atcf["basin"].str.pad(2)
        atcf["storm_number"] = atcf["storm_number"].astype("string").str.pad(3)
        atcf["datetime"] = atcf["datetime"].dt.strftime("%Y%m%d%H").str.pad(11)
        atcf["advisory_number"] = atcf["advisory_number"].str.pad(3)
        atcf["advisory"] = atcf["advisory"].str.pad(5)
        atcf["forecast_hours"] = atcf["forecast_hours"].astype("string").str.pad(4)

        atcf["latitude"] = atcf["latitude"].astype("string")
        atcf.loc[~atcf["latitude"].str.contains("-"), "latitude"] = (
            atcf.loc[~atcf["latitude"].str.contains("-"), "latitude"] + "N"
        )
        atcf.loc[atcf["latitude"].str.contains("-"), "latitude"] = (
            atcf.loc[atcf["latitude"].str.contains("-"), "latitude"] + "S"
        )
        atcf["latitude"] = atcf["latitude"].str.strip("-").str.pad(5)

        atcf["longitude"] = atcf["longitude"].astype("string")
        atcf.loc[~atcf["longitude"].str.contains("-"), "longitude"] = (
            atcf.loc[~atcf["longitude"].str.contains("-"), "longitude"] + "E"
        )
        atcf.loc[atcf["longitude"].str.contains("-"), "longitude"] = (
            atcf.loc[atcf["longitude"].str.contains("-"), "longitude"] + "W"
        )
        atcf["longitude"] = atcf["longitude"].str.strip("-").str.pad(6)

        atcf["max_sustained_wind_speed"] = (
            atcf["max_sustained_wind_speed"].astype("string").str.pad(4)
        )
        atcf["development_level"] = atcf["development_level"].str.pad(3)
        atcf["isotach_radius"] = atcf["isotach_radius"].astype("string").str.pad(4)
        atcf["isotach_quadrant_code"] = atcf["isotach_quadrant_code"].str.pad(4)
        atcf["isotach_radius_for_NEQ"] = (
            atcf["isotach_radius_for_NEQ"].astype("string").str.pad(5)
        )
        atcf["isotach_radius_for_SEQ"] = (
            atcf["isotach_radius_for_SEQ"].astype("string").str.pad(5)
        )
        atcf["isotach_radius_for_SWQ"] = (
            atcf["isotach_radius_for_SWQ"].astype("string").str.pad(5)
        )
        atcf["isotach_radius_for_NWQ"] = (
            atcf["isotach_radius_for_NWQ"].astype("string").str.pad(5)
        )

        atcf["background_pressure"] = atcf["background_pressure"].ffill().astype(int)
        atcf["central_pressure"] = atcf["central_pressure"].astype(int)

        press_cond_nobg = ~atcf["central_pressure"].isna() & (
            (atcf["background_pressure"] <= atcf["central_pressure"])
            | (atcf["background_pressure"].isna())
        )
        atcf.loc[press_cond_nobg, "background_pressure"] = 1013

        press_cond_nobg_hieye = press_cond_nobg & (atcf["central_pressure"] >= 1013)
        atcf.loc[press_cond_nobg_hieye, "background_pressure"] = (
            atcf.loc[press_cond_nobg_hieye, "central_pressure"] + 1
        )
        atcf["central_pressure"] = atcf["central_pressure"].astype("string").str.pad(5)
        atcf["background_pressure"] = (
            atcf["background_pressure"].astype("string").str.pad(5)
        )

        atcf["radius_of_last_closed_isobar"] = (
            atcf["radius_of_last_closed_isobar"].astype("string").str.pad(5)
        )
        atcf["radius_of_maximum_winds"] = (
            atcf["radius_of_maximum_winds"].astype("string").str.pad(4)
        )
        atcf["gust_speed"] = atcf["gust_speed"].astype("string").str.pad(4)
        atcf["eye_diameter"] = atcf["eye_diameter"].astype("string").str.pad(4)
        atcf["subregion_code"] = atcf["subregion_code"].str.pad(4)
        atcf["maximum_wave_height"] = (
            atcf["maximum_wave_height"].astype("string").str.pad(4)
        )
        atcf["forecaster_initials"] = atcf["forecaster_initials"].str.pad(4)

        atcf["direction"] = atcf["direction"].astype("string").str.pad(4)
        atcf["speed"] = atcf["speed"].astype("string").str.pad(4)
        atcf["name"] = atcf["name"].astype("string").str.pad(11)

        if "depth_code" in atcf.columns:
            atcf["depth_code"] = atcf["depth_code"].astype("string").str.pad(2)
            atcf["isowave"] = atcf["isowave"].astype("string").str.pad(3)
            atcf["isowave_quadrant_code"] = (
                atcf["isowave_quadrant_code"].astype("string").str.pad(4)
            )
            atcf["isowave_radius_for_NEQ"] = (
                atcf["isowave_radius_for_NEQ"].astype("string").str.pad(5)
            )
            atcf["isowave_radius_for_SEQ"] = (
                atcf["isowave_radius_for_SEQ"].astype("string").str.pad(5)
            )
            atcf["isowave_radius_for_SWQ"] = (
                atcf["isowave_radius_for_SWQ"].astype("string").str.pad(5)
            )
            atcf["isowave_radius_for_NWQ"] = (
                atcf["isowave_radius_for_NWQ"].astype("string").str.pad(5)
            )

        for column in atcf.select_dtypes(include=["string"]).columns:
            atcf[column] = atcf[column].str.replace(
                re.compile(str(integer_na_value)), "", regex=True
            )

        return atcf

    def fort_22(self, advisory: ATCF_Advisory = None) -> DataFrame:
        """
        https://wiki.adcirc.org/wiki/Fort.22_file

        :param advisory: advisory type
        :return: `fort.22` representation of the current track
        """

        fort22 = self.atcf(advisory=advisory)

        fort22.drop(
            columns=[
                field for field in EXTRA_ATCF_FIELDS.values() if field in fort22.columns
            ],
            inplace=True,
        )

        fort22["record_number"] = (
            (self.data.groupby(["datetime"]).ngroup() + 1).astype("string").str.pad(4)
        )

        if advisory == ATCF_Advisory.BEST or advisory == ATCF_Advisory.BEST.value:
            fort22["forecast_hours"] = (
                (
                    (self.data["datetime"] - self.data["datetime"].iloc[0])
                    / Timedelta("1 hour")
                )
                .astype(int)
                .astype("string")
                .str.pad(4)
            )

        return fort22

    @property
    def linestrings(self) -> Dict[str, Dict[str, LineString]]:
        """
        :return: spatial linestrings for every advisory and track
        """

        configuration = self.__configuration

        # only proceed if the configuration has changed
        if (
            self.__linestrings is None
            or len(self.__linestrings) == 0
            or configuration != self.__previous_configuration
        ):
            tracks = self.tracks

            linestrings = {}
            for advisory, advisory_tracks in tracks.items():
                linestrings[advisory] = {}
                for track_start_time, track in advisory_tracks.items():
                    geometries = track["geometry"]
                    if len(geometries) > 1:
                        geometries = geometries.drop_duplicates()
                        if len(geometries) > 1:
                            linestrings[advisory][track_start_time] = LineString(
                                geometries.to_list()
                            )

            self.__linestrings = linestrings

        return self.__linestrings

    @property
    def distances(self) -> Dict[str, Dict[str, float]]:
        """
        :return: length, in meters, of the track over WGS84 (``EPSG:4326``)
        """

        configuration = self.__configuration

        # only proceed if the configuration has changed
        if (
            self.__distances is None
            or len(self.__distances) == 0
            or configuration != self.__previous_configuration
        ):
            geodetic = Geod(ellps="WGS84")

            linestrings = self.linestrings

            distances = {}
            for advisory, advisory_tracks in linestrings.items():
                distances[advisory] = {}
                for track_start_time, linestring in advisory_tracks.items():
                    x, y = linestring.xy
                    _, _, track_distances = geodetic.inv(
                        x[:-1],
                        y[:-1],
                        x[1:],
                        y[1:],
                    )
                    distances[advisory][track_start_time] = numpy.sum(track_distances)

            self.__distances = distances

        return self.__distances

    def isotachs(
        self, wind_speed: float, segments: int = 91
    ) -> Dict[str, Dict[str, Dict[str, Polygon]]]:
        """
        isotach at the given wind speed at every time in the dataset

        :param wind_speed: wind speed to extract (in knots)
        :param segments: number of discretization points per quadrant
        :return: list of isotachs as polygons for each advisory type and individual track
        """

        valid_isotach_values = [34, 50, 64]
        assert (
            wind_speed in valid_isotach_values
        ), f"isotach must be one of {valid_isotach_values}"

        # collect the attributes needed from the forcing to generate swath
        data = self.data[self.data["isotach_radius"] == wind_speed]

        # enumerate quadrants
        quadrant_names = [
            "isotach_radius_for_NEQ",
            "isotach_radius_for_SEQ",
            "isotach_radius_for_SWQ",
            "isotach_radius_for_NWQ",
        ]

        # convert quadrant radii from nautical miles to meters
        data[quadrant_names] *= 1852.0

        geodetic = Geod(ellps="WGS84")

        tracks = separate_tracks(data)

        # generate overall swath based on the desired isotach
        isotachs = {}
        for advisory, advisory_tracks in tracks.items():
            advisory_isotachs = {}
            for track_start_time, track_data in advisory_tracks.items():
                track_isotachs = {}
                for index, row in track_data.iterrows():
                    # get the starting angle range for NEQ based on storm direction
                    rotation_angle = 360 - row["direction"]
                    start_angle = 0 + rotation_angle
                    end_angle = 90 + rotation_angle

                    # append quadrants in clockwise direction from NEQ
                    quadrants = []
                    for quadrant_name in quadrant_names:
                        # skip if quadrant radius is zero
                        if row[quadrant_name] > 1:
                            # enter the angle range for this quadrant
                            theta = numpy.linspace(start_angle, end_angle, segments)

                            # move angle to next quadrant
                            start_angle = start_angle + 90
                            end_angle = end_angle + 90

                            # make the coordinate list for this quadrant using forward geodetic (origin,angle,dist)
                            vectorized_forward_geodetic = numpy.vectorize(
                                partial(
                                    geodetic.fwd,
                                    lons=row["longitude"],
                                    lats=row["latitude"],
                                    dist=row[quadrant_name],
                                )
                            )
                            x, y, reverse_azimuth = vectorized_forward_geodetic(
                                az=theta
                            )
                            vertices = numpy.stack([x, y], axis=1)

                            # insert center point at beginning and end of list
                            vertices = numpy.concatenate(
                                [
                                    row[["longitude", "latitude"]].values[None, :],
                                    vertices,
                                    row[["longitude", "latitude"]].values[None, :],
                                ],
                                axis=0,
                            ).astype(float)

                            quadrants.append(Polygon(vertices))

                    if len(quadrants) > 0:
                        isotach = ops.unary_union(quadrants)

                        if isinstance(isotach, MultiPolygon):
                            isotach = isotach.buffer(1e-10)

                        track_isotachs[f'{row["datetime"]}:%Y%m%dT%H%M%S'] = isotach
                if len(track_isotachs) > 0:
                    advisory_isotachs[track_start_time] = track_isotachs
            if len(advisory_isotachs) > 0:
                isotachs[advisory] = advisory_isotachs
        return isotachs

    def wind_swaths(
        self, wind_speed: int, segments: int = 91
    ) -> Dict[str, Dict[str, Polygon]]:
        """
        wind swaths (per advisory type) for each advisory and track, as polygons

        :param wind_speed: wind speed in knots (one of ``34``, ``50``, or ``64``)
        :param segments: number of discretization points per quadrant (default = ``91``)
        """

        isotachs = self.isotachs(wind_speed=wind_speed, segments=segments)

        wind_swaths = {}
        for advisory, advisory_isotachs in isotachs.items():
            advisory_wind_swaths = {}
            for track_start_time, track_isotachs in advisory_isotachs.items():
                convex_hulls = []
                isotach_times = list(track_isotachs)
                for index in range(len(isotach_times) - 1):
                    convex_hulls.append(
                        ops.unary_union(
                            [
                                track_isotachs[isotach_times[index]],
                                track_isotachs[isotach_times[index + 1]],
                            ]
                        ).convex_hull
                    )

                if len(convex_hulls) > 0:
                    # get the union of polygons
                    advisory_wind_swaths[track_start_time] = ops.unary_union(
                        convex_hulls
                    )
            if len(advisory_isotachs) > 0:
                wind_swaths[advisory] = advisory_wind_swaths

        return wind_swaths

    @property
    def tracks(self) -> Dict[str, Dict[str, DataFrame]]:
        """
        :return: individual tracks sorted into advisories and initial times
        """
        return separate_tracks(self.data)

    @property
    def duration(self) -> pandas.Timedelta:
        """
        :return: duration of current track
        """

        return self.data["datetime"].diff().sum()

    @property
    def unfiltered_data(self) -> DataFrame:
        """
        :return: data frame containing all track data for the specified storm and file deck; NOTE: datetimes for forecasts represent the initial datetime of the forecast, not the datetime of the record
        """

        configuration = self.__configuration

        # only proceed if the configuration has changed
        if (
            self.__unfiltered_data is None
            or len(self.__unfiltered_data) == 0
            or configuration != self.__previous_configuration
        ):
            advisories = self.advisories
            if configuration["filename"] is not None:
                atcf_file = configuration["filename"]
            else:
                url = atcf_url(self.nhc_code, self.file_deck)
                try:
                    response = urlopen(url)
                except URLError:
                    url = atcf_url(
                        self.nhc_code, self.file_deck, mode=ATCF_Mode.HISTORICAL
                    )
                    try:
                        response = urlopen(url)
                    except URLError:
                        raise ConnectionError(f"could not connect to {url}")
                atcf_file = io.BytesIO()
                atcf_file.write(response.read())
                atcf_file.seek(0)
                if url.endswith(".gz"):
                    atcf_file = gzip.GzipFile(fileobj=atcf_file, mode="rb")

            if "OFCL" in advisories and "CARQ" not in advisories:
                self.__advisories_to_remove.append(ATCF_Advisory.CARQ)

            dataframe = read_atcf(
                atcf_file, advisories=advisories + self.__advisories_to_remove
            )
            dataframe.sort_values(["datetime", "advisory"], inplace=True)
            dataframe.reset_index(inplace=True, drop=True)

            dataframe["track_start_time"] = dataframe["datetime"].copy()
            if ATCF_Advisory.BEST in self.advisories:
                dataframe.loc[dataframe["advisory"] == "BEST", "track_start_time"] = (
                    dataframe.loc[dataframe["advisory"] == "BEST", "datetime"]
                    .sort_values()
                    .iloc[0]
                )

            dataframe.loc[
                dataframe["advisory"] != "BEST", "datetime"
            ] += pandas.to_timedelta(
                dataframe.loc[dataframe["advisory"] != "BEST", "forecast_hours"].astype(
                    int
                ),
                unit="hours",
            )

            self.unfiltered_data = dataframe
            self.__previous_configuration = configuration

        # if location values have changed, recompute velocity
        location_hash = pandas.util.hash_pandas_object(
            self.__unfiltered_data["geometry"]
        )

        if self.__location_hash is None or len(location_hash) != len(
            self.__location_hash
        ):
            updated_locations = ~self.__unfiltered_data.index.isnull()
        else:
            updated_locations = location_hash != self.__location_hash
        updated_locations |= pandas.isna(self.__unfiltered_data["speed"])

        if updated_locations.any():
            self.__unfiltered_data.loc[updated_locations] = self.__compute_velocity(
                self.__unfiltered_data[updated_locations]
            )
            self.__location_hash = location_hash

        return self.__unfiltered_data

    @unfiltered_data.setter
    def unfiltered_data(self, dataframe: DataFrame):
        # fill missing values of MRD and MSLP in the OFCL advisory
        if "OFCL" in self.advisories:
            tracks = separate_tracks(dataframe)
            if all(adv in tracks for adv in ["OFCL", "CARQ"]):
                tracks = correct_ofcl_based_on_carq_n_hollandb(
                    tracks, rmw_fill=self.rmw_fill
                )
                dataframe = combine_tracks(tracks)

        if len(self.__advisories_to_remove) > 0:
            dataframe = dataframe[
                ~dataframe["advisory"].isin(
                    [value.value for value in self.__advisories_to_remove]
                )
            ]
            self.__advisories_to_remove = []

        self.__unfiltered_data = dataframe

    @property
    def __configuration(self) -> Dict[str, Any]:
        return {
            "id": self.nhc_code,
            "file_deck": self.file_deck,
            "advisories": self.advisories,
            "filename": self.filename,
            "rmw_fill": self.rmw_fill,
        }

    @staticmethod
    def __compute_velocity(data: DataFrame) -> DataFrame:
        geodetic = Geod(ellps="WGS84")

        for advisory in pandas.unique(data["advisory"]):
            advisory_data = data.loc[data["advisory"] == advisory]

            indices = advisory_data.index
            shifted_indices = numpy.roll(indices, 1)
            shifted_indices[0] = indices[0]

            # check for negative time shifts which indicate new forecasts
            # and update this with the last previously available time
            for counter, ind in enumerate(zip(indices, shifted_indices)):
                this_time = advisory_data.loc[ind[0], "datetime"]
                shift_time = advisory_data.loc[ind[1], "datetime"]
                if shift_time > this_time:
                    # update shift index
                    if (advisory_data["datetime"] < this_time).sum() == 0:
                        shifted_indices[counter] = advisory_data["datetime"][
                            advisory_data["datetime"] > this_time
                        ].index[0]
                    else:
                        shifted_indices[counter] = advisory_data["datetime"][
                            advisory_data["datetime"] < this_time
                        ].index[-1]

            forward_azimuths, inverse_azimuths, distances = geodetic.inv(
                advisory_data.loc[indices, "longitude"],
                advisory_data.loc[indices, "latitude"],
                advisory_data.loc[shifted_indices, "longitude"],
                advisory_data.loc[shifted_indices, "latitude"],
            )

            intervals = (
                (
                    advisory_data.loc[indices, "datetime"].values
                    - advisory_data.loc[shifted_indices, "datetime"].values
                )
                .astype("timedelta64[s]")
                .astype(float)
            )
            speeds = pandas.Series(distances / abs(intervals), index=indices)
            bearings = pandas.Series(inverse_azimuths % 360, index=indices)
            # use forward azimuths for negative intervals
            bearings[intervals < 0] = pandas.Series(
                forward_azimuths[intervals < 0] % 360, index=indices[intervals < 0]
            )
            bearings[pandas.isna(speeds)] = numpy.nan
            # fill in nans carrying forward, because it is same valid time
            # and forecast but different isotach.
            # then fill nans backwards to handle the first time
            speeds.ffill(inplace=True)
            bearings.ffill(inplace=True)
            speeds.bfill(inplace=True)
            bearings.bfill(inplace=True)
            advisory_data["speed"] = speeds
            advisory_data["direction"] = bearings

            data.loc[data["advisory"] == advisory] = advisory_data

        return data

    @property
    def __file_end_date(self):
        unique_dates = numpy.unique(self.unfiltered_data["datetime"])
        for date in unique_dates:
            if date >= numpy.datetime64(self.end_date):
                return date

    def __len__(self) -> int:
        return len(self.data)

    def __copy__(self) -> "VortexTrack":
        instance = self.__class__(
            storm=self.unfiltered_data.copy(),
            start_date=self.start_date,
            end_date=self.end_date,
            file_deck=self.file_deck,
            advisories=self.advisories,
        )
        if self.filename is not None:
            instance.filename = self.filename
        return instance

    def __eq__(self, other: "VortexTrack") -> bool:
        return self.data.equals(other.data)

    def __str__(self) -> str:
        return f'{self.nhc_code} ({" + ".join(pandas.unique(self.data["advisory"]).tolist())}) track with {len(self)} entries, spanning {self.distances:.2f} meters over {self.duration}'

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({", ".join(repr(value) for value in [self.nhc_code, self.start_date, self.end_date, self.file_deck, self.advisories, self.filename])})'


class HollandBRelation:
    def __init__(self, rho: float = None):
        if rho is None:
            rho = 1.15
        self.rho = rho

    def holland_b(
        self,
        max_sustained_wind_speed: float,
        background_pressure: float,
        central_pressure: float,
    ) -> float:
        return ((max_sustained_wind_speed**2) * self.rho * numpy.exp(1)) / (
            background_pressure - central_pressure
        )

    def central_pressure(
        self,
        max_sustained_wind_speed: float,
        background_pressure: float,
        holland_b: float,
    ) -> float:
        return (
            -(max_sustained_wind_speed**2) * self.rho * numpy.exp(1)
        ) / holland_b + background_pressure

    def max_sustained_wind_speed(
        self,
        holland_b: float,
        background_pressure: float,
        central_pressure: float,
    ) -> float:
        return numpy.sqrt(
            holland_b
            * (background_pressure - central_pressure)
            / (self.rho * numpy.exp(1))
        )


def separate_tracks(data: DataFrame) -> Dict[str, Dict[str, DataFrame]]:
    """
    separate the given track data frame into advisories and tracks (forecasts / hindcasts)

    :param data: data frame of track
    :return: dictionary of forecasts for each advisory (aside from best track ``BEST``, which only has one hindcast)
    """

    tracks = {}
    for advisory in pandas.unique(data["advisory"]):
        advisory_data = data[data["advisory"] == advisory]
        advisory_data["forecast_hours"] = advisory_data.forecast_hours.astype(int)

        if advisory == "BEST":
            advisory_data = advisory_data.sort_values("datetime")

        track_start_times = pandas.unique(advisory_data["track_start_time"])

        tracks[advisory] = {}
        for track_start_time in track_start_times:
            if advisory == "BEST":
                track_data = advisory_data
            else:
                track_data = advisory_data[
                    advisory_data["track_start_time"]
                    == pandas.to_datetime(track_start_time)
                ].sort_index()

            tracks[advisory][
                f"{pandas.to_datetime(track_start_time):%Y%m%dT%H%M%S}"
            ] = track_data

    return tracks


def combine_tracks(tracks: Dict[str, Dict[str, DataFrame]]) -> DataFrame:
    """
    combine tracks separated using `separate_tracks`

    :param tracks: dictionary of forecasts for each advisory (aside from best track ``BEST``, which only has one hindcast)
    :return: data frame of track
    """

    return pandas.concat([df for adv_trk in tracks.values() for df in adv_trk.values()])


def correct_ofcl_based_on_carq_n_hollandb(
    tracks: Dict[str, Dict[str, DataFrame]],
    rmw_fill: RMWFillMethod = RMWFillMethod.regression_penny_2023,
) -> Dict[str, Dict[str, DataFrame]]:
    """
    Correct official forecast using consensus track along with holland-b
    relation

    :param tracks: dictionary of forecasts for each advisory (aside from best track ``BEST``, which only has one hindcast)
    :return: dictionary of forecasts for each advisory (aside from best track ``BEST``, which only has one hindcast) with corrected OFCL
    """

    def clamp(n, minn, maxn):
        return max(min(maxn, n), minn)

    def movingmean(dff):
        fcsthr_index = dff["forecast_hours"].drop_duplicates().index
        df_temp = dff.loc[fcsthr_index].copy()
        # make sure 60, 84, and 108 are added
        fcsthrs_12hr = numpy.unique(
            numpy.append(df_temp["forecast_hours"].values, [60, 84, 108])
        )
        rmw_12hr = numpy.interp(
            fcsthrs_12hr,
            df_temp["forecast_hours"],
            df_temp["radius_of_maximum_winds"],
        )
        dt_12hr = pandas.to_datetime(
            fcsthrs_12hr, unit="h", origin=df_temp["datetime"].iloc[0]
        )
        df_temp = DataFrame(
            data={
                "forecast_hours": fcsthrs_12hr,
                "radius_of_maximum_winds": rmw_12hr,
            },
            index=dt_12hr,
        )
        rmw_rolling = df_temp.rolling(window="24h1min", center=True, min_periods=1)[
            "radius_of_maximum_winds"
        ].mean()
        for valid_time, rmw in rmw_rolling.items():
            valid_index = dff["datetime"] == valid_time
            if (
                valid_index.sum() == 0
                or dff.loc[valid_index, "forecast_hours"].iloc[0] == 0
            ):
                continue
            # make sure rolling rmw is not larger than the maximum radii of the strongest isotach
            # this problem usually comes from the rolling average
            max_isotach_radii = isotach_radii.loc[valid_index].iloc[-1].max()
            if rmw < max_isotach_radii or numpy.isnan(max_isotach_radii):
                dff.loc[valid_index, "radius_of_maximum_winds"] = rmw
            # in case it does not come from rolling average just set to be Vr/Vmax ratio of max_isotach_radii
            if (
                dff.loc[valid_index, "radius_of_maximum_winds"].iloc[-1]
                > max_isotach_radii
            ):
                dff.loc[valid_index, "radius_of_maximum_winds"] = (
                    max_isotach_radii
                    * dff.loc[valid_index, "isotach_radius"].iloc[-1]
                    / dff.loc[valid_index, "max_sustained_wind_speed"].iloc[-1]
                )
        return dff

    ofcl_tracks = tracks["OFCL"]
    carq_tracks = tracks["CARQ"]

    corr_ofcl_tracks = dict()

    for initial_time, forecast in ofcl_tracks.items():
        if initial_time in carq_tracks:
            carq_forecast = carq_tracks[initial_time]
        else:
            carq_forecast = carq_tracks[list(carq_tracks)[0]]

        relation = HollandBRelation()
        holland_b = relation.holland_b(
            max_sustained_wind_speed=carq_forecast["max_sustained_wind_speed"],
            background_pressure=carq_forecast["background_pressure"],
            central_pressure=carq_forecast["central_pressure"],
        )

        holland_b[holland_b == numpy.inf] = numpy.nan
        holland_b = numpy.nanmean(holland_b)

        # Get CARQ from forecast hour 0 and isotach 34kt (i.e. the first item)
        carq_ref = carq_forecast.loc[carq_forecast.forecast_hours == 0].iloc[0]

        columns_of_interest = forecast[
            ["radius_of_maximum_winds", "central_pressure", "background_pressure"]
        ]
        columns_of_interest[columns_of_interest == 0] = pandas.NA
        missing = columns_of_interest.isna()
        # Order of columns is the same as columns_of_interest
        mrd_missing = missing.iloc[:, 0]
        mslp_missing = missing.iloc[:, 1]
        radp_missing = missing.iloc[:, 2]

        if rmw_fill == RMWFillMethod.persistent:
            # fill OFCL maximum wind radius with the first entry from
            # 0-hr CARQ
            forecast.loc[mrd_missing, "radius_of_maximum_winds"] = carq_ref[
                "radius_of_maximum_winds"
            ]

        elif (
            rmw_fill == RMWFillMethod.regression_penny_2023_with_smoothing
            or rmw_fill == RMWFillMethod.regression_penny_2023_no_smoothing
        ):
            # fill OFCL maximum wind radius based on regression method from
            # Penny et al. (2023). https://doi.org/10.1175/WAF-D-22-0209.1
            isotach_radii = forecast[
                [
                    "isotach_radius_for_NEQ",
                    "isotach_radius_for_SEQ",
                    "isotach_radius_for_NWQ",
                    "isotach_radius_for_SWQ",
                ]
            ]
            isotach_radii[isotach_radii == 0] = pandas.NA
            rmw0 = carq_ref["radius_of_maximum_winds"]
            fcst_hrs = (forecast.loc[mrd_missing, "forecast_hours"]).unique()
            rads = numpy.array([numpy.nan])  # initializing to make sure available
            rads_bias_names = [
                c for c in RMW_bias_correction.columns if "isotach_radius" in c
            ]
            for fcst_hr in fcst_hrs:
                fcst_index = forecast["forecast_hours"] == fcst_hr
                if fcst_hr < 12:
                    rmw_ = rmw0
                else:
                    fcst_hr_bc = min(fcst_hr, 120)
                    vmax = forecast.loc[fcst_index, "max_sustained_wind_speed"].iloc[0]
                    vmax -= RMW_bias_correction["max_sustained_wind_speed"][fcst_hr_bc]
                    if numpy.isnan(isotach_radii.loc[fcst_index].to_numpy()).all():
                        # if no isotach's are found, preserve the isotach(s) if Vmax is greater
                        if vmax > 50:
                            rads = rads[0 : min(2, len(rads))]
                        elif vmax > 34:
                            rads = rads[[0]]
                        else:
                            rads = numpy.array([numpy.nan])
                    else:
                        rads = numpy.nanmean(
                            isotach_radii.loc[fcst_index].to_numpy(), axis=1
                        )
                        rads -= (
                            RMW_bias_correction[rads_bias_names[0 : rads.size]]
                            .loc[fcst_hr_bc]
                            .values
                        )
                    coefs = get_RMW_regression_coefs(fcst_hr, rads)
                    lat = forecast.loc[fcst_index, "latitude"].iloc[0]
                    lat -= RMW_bias_correction["latitude"][fcst_hr_bc]
                    bases = numpy.hstack(
                        (1.0, rmw0, rads[~numpy.isnan(rads)], vmax, lat)
                    )
                    rmw_ = (bases[1:-1] ** coefs[1:-1]).prod() * numpy.exp(
                        (coefs[[0, -1]] * bases[[0, -1]]).sum()
                    )  # bound RMW as per Penny et al. (2023)
                forecast.loc[fcst_index, "radius_of_maximum_winds"] = clamp(
                    rmw_, 5.0, max(120.0, rmw0)
                )
            # apply 24-HR moving mean to unique datetimes
            if rmw_fill == RMWFillMethod.regression_penny_2023_with_smoothing:
                forecast = movingmean(forecast)

        # fill OFCL background pressure with the first entry from 0-hr CARQ background pressure (at sea level)
        forecast.loc[radp_missing, "background_pressure"] = carq_ref[
            "background_pressure"
        ]

        # fill OFCL central pressure (at sea level), preserving Holland B from 0-hr CARQ
        forecast.loc[mslp_missing, "central_pressure"] = relation.central_pressure(
            max_sustained_wind_speed=forecast.loc[
                mslp_missing, "max_sustained_wind_speed"
            ],
            background_pressure=forecast.loc[mslp_missing, "background_pressure"],
            holland_b=holland_b,
        )

        corr_ofcl_tracks[initial_time] = forecast

    tracks["OFCL"] = corr_ofcl_tracks

    return tracks
