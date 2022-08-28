#!/usr/bin/env python3

_PROGRAM_NAME = 'exiflabeler'
_DESCRIPTION = '\
An EXIF metadata manager that supports better handling for timestamp, timezone, device, author metadata.\n\
This program can be used to perform the following tasks:\n\
(1) Manage device information (e.g. historical timezone setup) using a config database.\n\
(2) Convert EXIF tags to filenames and vice versa (limited to the aforementioned tags), using data supplied through the config file and the command line to resolve missing/incorrect information.\n\
'

# Notes
# * exif is the main supported format, thanks to the ubiquity of the
#   DateTimeOriginal field (which is intended to record the original time of
#   recording)
# * this is less feasible for other formats, which may not have a distinct
#   "creation time" timestamp used specifically for this purpose
#   (e.g. isom's "encoded time")
#
# internals
# Note: the name "exif" is overloaded - it can refer to the data format, or specifically the EXIF sub-IFD

import os
import os.path
import argparse
import atexit

from collections import namedtuple
import functools
import re
from csv import reader as CsvReader
from datetime import datetime, timezone, timedelta
import pytz

print = functools.partial(print, flush=True)

# Note: most of the time, should only use if interactive=True
def input_prefill(prompt, prefill=''):
    import readline
    readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()

# WARNING: should always use get_timezone('UTC') instead of timezone.utc for compatibility with pytz timezones
def get_timezone(tzname):
    FIXED_OFFSET_PREFIXES = ['UTC', 'GMT']
    for p in FIXED_OFFSET_PREFIXES:
        if not tzname.startswith(p):
            continue
        offset_str = tzname[len(p):]
        # pytz.FixedOffset only supports whole-minute offsets # TODO dateutil migration
        if not offset_str:
            offset_minutes = 0
        else:
            offset_minutes = datetime.strptime(offset_str, '%z').utcoffset().total_seconds() / 60
        tz = pytz.FixedOffset(offset_minutes)
        #tz.zone = 'UTC{0:+}'.format(offset_hours)
        tz.zone = 'UTC' + offset_str
        return tz
    return pytz.timezone(tzname)

#
# image & exif
#

EXIF_TIMESTAMP_FORMAT = '%Y:%m:%d %H:%M:%S'

def parse_timestamp(localtime_str):
    try:
        localtime = datetime.strptime(localtime_str, EXIF_TIMESTAMP_FORMAT)
    except:
        # fix dependencies' internal error where 00 seconds is printed as empty string (occurs on older versions)
        localtime = datetime.strptime(localtime_str, '%Y:%m:%d %H:%M:  ')
        raise
    return localtime

# dt must be timezone-aware object
def print_timestamp(dt, *, heading, indent_level):
    # is_dst (None represents "unambiguous") needed to distinguish "no ambiguity" from "would have been ambiguous but 'no dst' was selected"
    is_dst = None
    try:
        dt.tzinfo.localize(dt.replace(tzinfo=None), is_dst=None)
    except pytz.exceptions.AmbiguousTimeError:
        is_dst = bool(dt.dst()) # warning: pytz does not support fold() # TODO - phase out pytz later?
    # new_dt isn't technically localtime since it still includes timezone info, but it can be used as localtime
    return print_timestamp_explicit(dt, dt.tzinfo, is_dst,
        heading=heading, indent_level=indent_level)

# dt must be timezone-aware object
def print_timestamp_in(dt, tz, *, heading, indent_level):
    return print_timestamp(dt.astimezone(tz),
        heading=heading, indent_level=indent_level)

def print_timestamp_explicit(localtime, tz, is_dst, *, heading, indent_level):
    is_dst_str = '' if is_dst is None else ' (DST=%s)' %is_dst
    print('\t' * indent_level + heading + localtime.strftime(EXIF_TIMESTAMP_FORMAT) + is_dst_str)

#class ImageInfo:
#    pass

class ImageInfo_pillow:
    _name = 'Pillow (PIL fork)'
    try:
        import PIL.Image
        import PIL.ExifTags
        PIL_EXIF_TAGNAME_MAP = { v: k for k, v in PIL.ExifTags.TAGS.items() }
        PIL_GPS_TAGNAME_MAP = { v: k for k, v in PIL.ExifTags.GPSTAGS.items() }
        _supported = True
        print('[Installed: %s]' %_name)
    except ModuleNotFoundError:
        _supported = False

    def __init__(self, path):
        self.path = path
        self.img = None

    # lifecycle
    # refer to https://pillow.readthedocs.io/en/stable/reference/open_files.html#image-lifecycle
    def __enter__(self):
        self.img = self.PIL.Image.open(path)
        return self

    def __exit__(self, *_):
        self.img.close()

    # TODO depr - exif only or all data? note that this is a lot more limited than exiftool's "full dump"
    def _info(self): return self._exifinfo()

    def _exifinfo(self):
        # internally cached by PIL
        return self.img._getexif()

    def dimensions(self):
        return self.img.size

    def get_exif_value(self, key):
        # https://pillow.readthedocs.io/en/stable/reference/Image.html?highlight=_getexif#PIL.Image.Exif
        # NOTE:
        # starting with v8.2.0, EXIF sub-IFD is no longer accessible as if flattened into the main dict;
        # now only accessible via `getexif().get_ifd(0x8769)`
        # https://pillow.readthedocs.io/en/stable/releasenotes/8.2.0.html#image-getexif-exif-and-gps-ifd
        return self._exifinfo().get(self.PIL_EXIF_TAGNAME_MAP[key])

    def get_gps_value(self, key):
        return self._exifinfo().get(self.PIL_GPS_TAGNAME_MAP[key])

    def get_tag(self, key):
        namespace, basename = key.split(':')
        if namespace == 'EXIF':
            return get_exif_value(basename)
        elif namespace == 'GPS':
            return get_gps_value(basename)
        else:
            raise NotImplementedError("Tag not supported: %s" %key)

# shared by all exiftool-based implementations
class ImageInfo_exiftool:
    def _get_tag(key, func): raise NotImplementedError()

    def dimensions(self):
        return (
            self._get_tag('ImageWidth'),
            self._get_tag('ImageHeight'),
        )

    def get_exif_value(self, exif_key):
        return self.get_tag('EXIF:' + exif_key)

    def get_gps_value(self, exif_key):
        return self.get_tag('GPS:' + exif_key)

    def get_tag(self, key):
        def adjust_keyname(key):
            # TODO - INTERIM - how to establish full mapping
            if key == 'EXIF:SubsecTimeOriginal': return 'EXIF:SubSecTimeOriginal'
            return key
        def adjust_keyvalue(key, val_str):
            if key == 'EXIF:SubsecTimeOriginal':
                # NOTE: fixup to resolve type error - should be string since it needs to express value with precision (caused by exiftool's json output code)
                # * if subsec has leading zeroes, result is string
                # * if subsec has no leading zeroes, result is int
                if isinstance(val_str, int):
                    return str(val_str)
            return val_str
        return adjust_keyvalue(key, self._get_tag(adjust_keyname(key)))

class ImageInfo_pyexiftool(ImageInfo_exiftool):
    _name = 'pyexiftool'
    try:
        import exiftool
        # globally-shared instance
        _tool = exiftool.ExifTool()
        _supported = True
        print('[Installed: %s]' %_name)
    except ModuleNotFoundError:
        _supported = False

    _tool_inited = False
    def _tool_init():
        # once started, exiftool will remain in the background until the main program terminates.
        if not ImageInfo_pyexiftool._tool_inited:
            ImageInfo_pyexiftool._tool_inited = True
            atexit.register(ImageInfo_pyexiftool._tool.terminate)
            ImageInfo_pyexiftool._tool.start()

    def __init__(self, path):
        self.path = path
        self._cached = None
        ImageInfo_pyexiftool._tool_init()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def _info(self):
        if self._cached is None:
            self._cached = self._tool.get_metadata(self.path)
        return self._cached

    def _get_tag(self, key):
        return ImageInfo_pyexiftool._tool.get_tag(key, self.path)

class ImageInfo_pyexifinfo(ImageInfo_exiftool):
    _name = 'pyexifinfo'
    try:
        import pyexifinfo
        _supported = True
        print('[Installed: %s]' %_name)
    except ModuleNotFoundError:
        _supported = False

    def __init__(self, path):
        self.path = path
        self._cached = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def _info(self):
        if self._cached is None:
            import pyexifinfo
            self._cached = pyexifinfo.information(self.path) or False
        return self._cached

    def _get_tag(self, key):
        return self._info().get(key)

# dummy class to capture error information
class ImageInfo_unsupported:
    def __init__(self, path, parsers):
        self.path = path
        self.parsers = parsers

    def msg(self):
        return 'No supported parser is installed (%s) for file (%s)' %(self.parsers, path)

    def throw(self):
        raise NotImplementedError(self.msg())

    def __enter__(self):
        self.throw()

    def __exit__(self, *_):
        self.throw()

def ImageInfo(path, require_write=False):
    def get_supported_parsers(path, require_write):
        #
        # command-line
        #
        if require_write or os.environ.get('USE_PILLOW') == '0':
            parsers = [ImageInfo_pyexiftool, ImageInfo_pyexifinfo]
        elif os.environ.get('USE_PILLOW') == '1':
            parsers = [ImageInfo_pillow]
        else:
            parsers = [ImageInfo_pillow, ImageInfo_pyexiftool, ImageInfo_pyexifinfo]
        #
        # file formats
        #
        _, dot_ext = os.path.splitext(path)
        # TODO: extend using a filetype library (see https://stackoverflow.com/questions/10937350/how-to-check-type-of-files-without-extensions), e.g. filetype, magic
        # TODO: other formats? also consider coding capabilities into file parser classes instead (too difficult?)
        if dot_ext.lower() == '.heic':
            parsers = [p for p in parsers if p != ImageInfo_pillow]

        return parsers

    parsers = get_supported_parsers(path, require_write)
    for cImageInfo in parsers:
        if cImageInfo._supported:
            #print(cImageInfo) # debug print
            return cImageInfo(path)
    return ImageInfo_unsupported(parsers, path)

#
# source device management
#

# csv format - rows:
# device_make, device_model, device_id
# returns: map from (device_make, device_model) to device_id
def load_device_names(path):
    name_map = {}
    if not os.path.isfile(path):
        return {}
    with open(path, mode='r') as file:
        for line in file:
            if not line or line[0] == '#': continue
            columns = [val.strip() for val in line.split(',')]
            name_map[(columns[0], columns[1])] = columns[2]
    return name_map

def save_device_names(path, name_map):
    print('[Begin updating device name map]')
    with open(path, mode='w') as file:
        for (device_make, device_model), device_id in name_map.items():
            file.write(device_make + ',' + device_model + ',' + device_id + '\n')
    print('[Successfully updated device name map]')

# representing a historical timezone config
DEVICE_TZINFO_FIELDS = ['tz', 'start', 'end']
class DeviceTzCfgEntry(namedtuple('DeviceTzCfgEntry', DEVICE_TZINFO_FIELDS)):
    __slots__ = ()
    def __str__(self):
        return '%s (%s, %s)' %(self.tz, self.start, self.end or '-')

# csv format - rows:
# device_id, tzname, start time, end time (both in UTC, specified in ISO format with dashes)
# end is optional - if omitted, will assume to be start of next entry
# returns: map from device_id to list of (tz, start, end) tuples
def load_device_tzinfo(path):
    # format: { device_id : [ ( tz, start, end ), ... ], ... }
    # (list is sorted by start time)
    device_tzmap = {}
    if not path or not os.path.exists(path):
        return device_tzmap
    with open(path, mode='r') as file:
        reader = CsvReader(file)
        def parse_time(ds):
            if not ds: return None
            yr, mon, day, hr, min, *_ = [int(v) for v in ds.split('-')]
            return datetime(yr, mon, day, hr, min, tzinfo=get_timezone('UTC'))
        for device_id, tzname, start, end, *_ in reader:
            tz = get_timezone(tzname)
            tz_item = DeviceTzCfgEntry(tz, parse_time(start), parse_time(end))
            device_tzmap.setdefault(device_id, []).append(tz_item)
    for device_id, tz_list in device_tzmap.items():
        tz_list.sort(key=lambda tz_item: tz_item.start.timestamp())
        for index in range(1, len(tz_list)):
            prev = tz_list[index - 1]
            cur = tz_list[index]
            if prev.end > cur.start:
                print('[Loading device tzinfo for %s - Conflict detected: configured intervals overlap for %s, %s]' %(device_id, prev.tz, cur.tz))
    return device_tzmap

# return: [ (tzinfo, is_dst, datetime) ]
# is_dst: None - "unambiguous, will auto-detect" (this is why we aren't simply returning datetime, because it is unable to distinguish "no ambiguity" from "would have been ambiguous but 'no dst' was selected")
# localtime may be ambiguous - during some internal timezone transition localtime overlap is possible
# (e.g. PDT->PST under US/Pacific, or between custom timezones specified in the list)
# when this is the case, all possible timezones will be returned
def tz_interpret_localtime(tz, localtime):
    results = []
    def verify_add(tz, is_dst):
        #nonlocal localtime
        #nonlocal results
        t = tz.localize(localtime, is_dst=is_dst)
        results.append((t.tzinfo, is_dst, t))
    try:
        verify_add(tz, None)
    except pytz.exceptions.AmbiguousTimeError:
        verify_add(tz, False)
        verify_add(tz, True)
    return results

# return: [ (tzinfo, is_dst, datetime) ]
# ditto for is_dst, time ambiguities
def device_tzinfo_interpret_localtime(device_tzmap, device_id, localtime):
    results = [] # tzinfo, is_dst (None: auto iff unambiguous), datetime
    def verify_add(tz_info, is_dst):
        #nonlocal localtime
        #nonlocal results
        (tz, start, end) = tz_info
        t = tz.localize(localtime, is_dst=is_dst)
        if t >= start and (t < end if end else True):
            results.append((t.tzinfo, is_dst, t))
            return True
        return False
    for tz_info in device_tzmap.get(device_id, {}):
        try:
            verify_add(tz_info, None)
        except pytz.exceptions.AmbiguousTimeError:
            verify_add(tz_info, False)
            verify_add(tz_info, True)
    return results

#
# filename processing
#

# format:
# * mainname == (DSC|IMG|dsc|dSC)[_][<modified_flag>]<serial>[<modified_flag>]
# * followed by modifier suffix: [_<modifier>]
SERIAL_IMGNAME_CHECK = re.compile('(?P<mainname_prefix>([Dd][Ss][Cc]|[Ii][Mm][Gg]))_?(?P<modified_flag_pre>[E])?(?P<serial>[0-9]{1,5})(?P<modified_flag_post>[E])?(_(?P<modifier>[a-z0-9\(\)\[\],-]+))?')
#SerialImageNameInfo = namedtuple('SerialImageNameInfo', 'prefix', 'serial', 'modified')
class SerialImageNameInfo(
    namedtuple(
        'SerialImageNameInfo',
        (
            'prefix',
            'serial',
            'modified',
        )
    )
):
    def formatDescription(self):
        return "raw"

    def from_parse(regex, possible_modifier_override=False):
        possible_modifier = possible_modifier_override
        if (regex.group('modified_flag_pre') or
            regex.group('modified_flag_post')):
            possible_modifier = True
        return SerialImageNameInfo(
            prefix=regex.group('mainname_prefix'),
            serial=regex.group('serial'),
            modified=possible_modifier
        )

# "new" format, generated by this tool
# format:
# * mainname == DSC<timestamp>['<precision>][_<device_id>]
# * followed by modifier suffix: [_<modifier>]
# TODO: in newstyle filename, what if device is omitted?? modifier?? - currently distinguished by always having model name be uppercase and modifier be lowercase, but this is ambiguous on Windows and NTFS
STRUCTURED_IMGNAME_CHECK = re.compile('DSC(?P<timestamp>[0-9]{10})(_(?P<device_id>[A-Z][A-Z0-9]*))?(_(?P<modifier>[a-z0-9\(\)\[\],-]+))?')
#StructuredImageNameInfo = namedtuple('StructuredImageNameInfo', 'timestamp', 'timestamp_precision', 'device_id')
class StructuredImageNameInfo(
    namedtuple(
        'StructuredImageNameInfo',
        (
            'timestamp',
            'timestamp_precision',
            'device_id',
        )
    )
):
    def formatDescription(self):
        return _PROGRAM_NAME

    """
    if (
        len(mainname) >= 13
        and mainname.startswith("DSC")
        and mainname[3:13].isdigit()
        and (len(mainname) == 13 or not mainname[13].isdigit())
    ):
        name_parts = mainname.split("_")
        timestamp = int(basename[3:13]) # should be same as name_parts[0] with "DSC" prefix stripped
        device_id = name_parts[1]
        modifier = name_parts[2] if len(name_parts) >= 3 else ''
    """
    def from_parse(regex):
        return StructuredImageNameInfo(
            timestamp=int(regex.group('timestamp')),
            timestamp_precision=None, #regex.group('precision'), # TODO: handle precision & comparison
            device_id=regex.group('device_id'),
        )

# TODO - consider incorporating prefix, modifier into FilenameInfo
# de-facto factory function for ImageNameInfo
# overall format: [<prefix>( |_)]<mainname>[_<modifier>].<fmt_suffix>
# returns: prefix, nameinfo, modifier, ext
# mainname formats correspond to the following ImageNameInfo types:
# * auto-generated by device - SerialImageNameInfo
# * "new" type - StructuredImageNameInfo
# * unknown - None
def parse_filename(filename):
    # remove suffix, remove everything up to IMG/DSC
    #title, ext = os.path.splitext(basename); ext = ext[1:]
    title = '.'.join(basename.split('.')[:-1])
    ext = basename.split('.')[-1]
    mainname_prefix = None
    # TODO: also need to account for the fact that sometimes description is appended to the end
    for MAINNAME_PREFIX in ['DSC', 'IMG', 'dsc', 'dSC']:
        start_idx = basename.find(MAINNAME_PREFIX)
        if start_idx == -1: continue
        mainname_prefix = MAINNAME_PREFIX
        prefix = title[:start_idx]
        mainname = title[start_idx:]
        break
    else:
        # unknown format
        return None, title, None, ext

    structuredNameCheck = STRUCTURED_IMGNAME_CHECK.search(mainname)
    serialNameCheck = SERIAL_IMGNAME_CHECK.search(mainname)
    if structuredNameCheck:
        nameinfo = StructuredImageNameInfo.from_parse(structuredNameCheck)
        modifier = structuredNameCheck.group('modifier')
    elif serialNameCheck:
        possible_modifier = False
        if ext == 'jpg':
            possible_modifier = True # historical reasons
        nameinfo = SerialImageNameInfo.from_parse(serialNameCheck, possible_modifier)
        modifier = serialNameCheck.group('modifier')
        if possible_modifier and not modifier:
            modifier = True

    return prefix, nameinfo, modifier, ext

#
# interactive operations
#

def print_parse_filename(filename, *, disp_tz=None):
    prefix, nameinfo, modifier, ext = parse_filename(filename)
    print("\tFilename:Format: %s" %nameinfo.formatDescription())
    if isinstance(nameinfo, StructuredImageNameInfo):
        if modifier:
            print('\t\t(Detected "%s" suffix in new-style name)' %modifier)
        dt = datetime.fromtimestamp(nameinfo.timestamp, get_timezone('UTC'))
        print("\t\tFilename:Timestamp|UTC: %s" %(dt.strftime(EXIF_TIMESTAMP_FORMAT)))
        if (disp_tz
            and disp_tz != get_timezone('UTC')
            and disp_tz != timezone.utc # just in case we used the native tzinfo object for utc (which does not compare equal to the pytz object, at least historically)
        ):
            #print_timestamp_explicit(dt, disp_tz, is_dst,
            print_timestamp_in(dt, disp_tz,
                heading='Filename:Timestamp|DISP: ', indent_level=2)
    elif isinstance(nameinfo, SerialImageNameInfo):
        if modifier:
            if ext == 'jpg':
                print('\t\t(Detected lower case file extension)')
            elif not modifier is True:
                print('\t\t(Detected "%s" suffix in old-style name)' %modifier)
        dt = None
        pass
    else:
        print('\t(Unknown naming scheme)')
        dt = None
    return prefix, nameinfo, modifier, ext, dt

def get_print_exif_value(imginfo, key):
    value = imginfo.get_exif_value(key)
    print('\t' + key + ': ' + str(value))
    return value

def get_print_file_origin_timestamp(imginfo):
    localtime_str = get_print_exif_value(imginfo, 'DateTimeOriginal')
    if not localtime_str:
        return None, None
    subsec_str = get_print_exif_value(imginfo, 'SubsecTimeOriginal') or '' # might not exist
    subsec_str = subsec_str[:1] # truncate to 1 digit if longer, too much precision is useless (we keep remaining digits to distinguish between images taken within the same second)
    localtime = parse_timestamp(localtime_str)
    return localtime, subsec_str

# get abbreviation of device name
# None: no input
# False: error
def get_set_device_id_interactive(imginfo, *, interactive, cfgfile_device_names, device_names):
    make_str = get_print_exif_value(imginfo, 'Make')
    model_str = get_print_exif_value(imginfo, 'Model')
    if model_str:
        model_str = model_str.strip()
        device_id = device_names.get((make_str or '', model_str))
        if not device_id:
            device_id = device_names.get(('', model_str))
        if not device_id:
            if not interactive:
                print('\tNo abbreviation set for device model (%s)' %model_str)
                return False
            while True:
                device_id = input_prefill('\t-> Enter an abbreviation for this device model (%s):\n' %model_str)
                if device_id:
                    device_names[(make_str or '', model_str)] = device_id
                    save_device_names(cfgfile_device_names, device_names)
                    break
                else:
                    print('\tValue cannot be empty.')
        else:
            print('\t<ModelAbbr>: %s' %device_id)
    else:
        device_id = None
    return device_id

# TODO: consider splitting by area? (or just remove this entirely; replace with "check" or a similar one "view" instead) - note that currently this prints ALL metadata for exifinfo, not PIL
# TODO: new PIL IFD iteration problem
def print_exif(imginfo):
    # TO DO: print parser info first
    for key, val in imginfo._info().items():
        if isinstance(imginfo, ImageInfo_pillow): # PIL
            import PIL
            if key == ImageInfo_pillow.PIL_EXIF_TAGNAME_MAP['MakerNote']:
                # reason: not parsed by old versions of PIL, instead dumped as raw
                print('\tMakerNote (not supported)')
                continue
            if key in PIL.ExifTags.TAGS:
                print(f"\t{PIL.ExifTags.TAGS[key]}: {repr(val)}")
            elif key in PIL.ExifTags.GPSTAGS:
                print(f"\t{PIL.ExifTags.GPSTAGS[key]}: {repr(val)}")
        else: # exiftool
            print(f"\t{key}: {str(val)}")

# None for "unspecified", False for error
def interpret_localtime_interactive(
    localtime,
    *, interactive,
    device_tzmap, override_tz, device_id,
    print_all, require_unique, cand_callback,
):
    def print_tz_candidates(tz_candidates, cand_callback, *, heading, indent_level=1):
        indent = '\t' * indent_level
        if tz_candidates is None:
            return
        if len(tz_candidates) > 1:
            print('\t' * indent_level + heading + ': (ambiguous)')
            indent_level += 1
        for (device_tz, is_dst, dt) in tz_candidates:
            is_dst_str = '' if is_dst is None else ' (DST=%s)' %is_dst
            zone_expr = device_tz.zone + is_dst_str
            zone_name = device_tz.tzname(dt)
            print('\t' * indent_level
                + ((heading + ': ') if len(tz_candidates) == 1 else '')
                # print full name + abbreviated name
                + ('%s (%s)' %(zone_expr, zone_name) if zone_name
                    else zone_expr
                )
            )
            print_timestamp_explicit(dt, device_tz, is_dst,
                heading='DateTimeOriginal|PRESET: ', indent_level=indent_level+1)
            #print('\t' * (indent_level + 1)
            #    + 'DateTimeOriginal|PRESET: %s' %dt.strftime(EXIF_TIMESTAMP_FORMAT) + is_dst_str)
            cand_callback(device_tz, is_dst, dt, indent_level=indent_level)

    override_tz_candidates = tz_interpret_localtime(override_tz, localtime)\
        if override_tz else None
    cfgfile_tz_candidates = device_tzinfo_interpret_localtime(device_tzmap, device_id, localtime)\
        if device_id else None
    tz_candidates = override_tz_candidates or cfgfile_tz_candidates
    print_tz_candidates(tz_candidates, cand_callback,
        heading='LocaltimeInterpretation:Timezone')
    if override_tz and cfgfile_tz_candidates:
        if print_all:
            print_tz_candidates(cfgfile_tz_candidates, cand_callback,
                heading='LocaltimeInterpretation:Timezone (config file)')
        if not functools.reduce(
            lambda acc, x: (acc or x[0] == override_tz),
            cfgfile_tz_candidates,
            False,
        ): # if override_tz is not found in cfgfile_tz_candidates
            print('\t\t(Warning: specified timezone conflicts with device configuration)')

    if not tz_candidates: # implies not override_tz
        if not device_id in device_tzmap:
            print('\tLocaltimeInterpretation:Timezone: (none)\n'
                '\t\t(Warning: device has no configured timezones)')
            return None
        else:
            print('\tLocaltimeInterpretation:Timezone: (none)\n'
                '\t\t(Warning: timestamp is not within configured range)')
            return None
    if len(tz_candidates) == 1:
        return tz_candidates[0]
    else:
        if require_unique and interactive:
            while True:
                answer = input_prefill('\t\t-> Select timezone by entering 1-based numerical index:\n\t\t   ', '')
                if not answer:
                    return None
                ord = int(answer) if answer.isdigit() else -1
                if ord < 1 or ord > len(tz_candidates):
                    print('\t\t(Error: invalid index - Please enter a number between 1 and %s)' %len(tz_candidates))
                    continue
                result = tz_candidates[ord - 1]
                if interactive > 1:
                    print('\t\t(Selected: %s)' %result[0].zone)
                return result
        elif require_unique:
            return False
        else:
            return None

def interactive_file_rename(suggested_name, outdirname, *, interactive):
    import shutil
    first_prompt = True
    while True:
        if interactive > (1 if first_prompt else 0):
            accepted_name = input_prefill('\t-> Save to: ', suggested_name)
        else:
            accepted_name = suggested_name
        first_prompt = False
        dstpath = os.path.join(outdirname, accepted_name)

        # if path is cleared, assume user wants to skip the renaming
        if not accepted_name:
            break

        if accepted_name == basename:
            print("\t\tFile already has the desired name")
            break

        if (os.path.exists(dstpath)):
            print("\t\tError: destination already exists")
            if not interactive:
                return False
            continue

        try:
            shutil.move(path, dstpath)
        except shutil.Error as e:
            print(str(e))
            if not interactive:
                return False
            continue
        break
    return True

#
# command-line parsing & environment setup
#

#def main():
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=_DESCRIPTION,
        epilog='Supported timezone strings:\n(1) tzdb format (e.g. "Pacific/Pitcairn")\n(2) "UTC" suffixed with ISO-8601 offset (e.g. "UTC+0800")\n(3) "UTC"',
        #'Name formats' # TODO explain name formats
        #formatter_class=argparse.RawDescriptionHelpFormatter # TODO: wrap fix for header (should preserve double-newlines)
    )
    parser.add_argument(
        'paths',
        action='store', nargs='+',
        metavar='PATH',
        help='paths to files (glob patterns not currently accepted); for convenience, this may be specified at the end of the command line, in which case it must be preceded with the dummy argument "--".',
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='print more internal state & debug information.',
    )
    parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='when running a file-processing command, this will prevent any filesystem changes from being applied.',
    )
    parser.add_argument(
        '-i', '--interactive', '--prompt', # "--prompt" for historical reasons
        action='store', nargs='?',
        type=int, choices=[0, 1, 2], default=0, const=1,
        metavar='PROMPT_LEVEL',
        help='use interactive prompts to confirm actions. The following levels are supported:\n0 - non-interactive; 1 - prompt on warnings and (non-fatal) errors only; 2 - prompt on all actions (default is 0).',
    )
    actions_group = parser.add_argument_group('verbs')
    actions_group.add_argument(
        '--show', '-S',
        action='store_true',
        help='Display extended file information'
    )
    actions_group.add_argument(
        '--check', '-X',
        action='store_true',
        help='Check for name/metadata inconsistencies. If check fails, further actions will be cancelled.'
        #action='store', nargs='?',
        #type=float, const='10',
        #metavar='PRECISION',
        #...'optionally specifying a precision value (measured in seconds)'
    )
    mutator_actions_group = actions_group.add_mutually_exclusive_group()
    mutator_actions_group.add_argument(
        '--rename', '-N',
        action='store_true',
        help='Rename file'
        #action='store', nargs='?', const='std',
        #metavar='FMT_NAME|FMT_PATTERN',
        #help='Rename file using the specified name pattern (by name or by pattern string)'
    )
    #mutator_actions_group.add_argument(
    #    '--retag', '-T'
    #    action='store', nargs='?', const='localtime,timezone',
    #    metavar='ATTRIB_LIST',
    #    help='Set metadata attributes (specified as comma-separated list) based on filename'
    #)
    data_params_group = parser.add_argument_group('data parameters')
    data_params_group.add_argument(
        '--cfgdir',#'--cfgfile'
        action='store',
        metavar='CFG_DIR',
        help='path to directory in which config files are located. If omitted, user\'s home directory will be used.',
    )
    data_params_group.add_argument(
        '--src-tz', '-s',
        action='store',
        metavar='TIMEZONE',
        help='specify the timezone for interpreting stored timestamps, overriding device preset values (required when performing rename; optional otherwise).',
    )
    data_params_group.add_argument(
        '--disp-tz',
        action='store',
        metavar='TIMEZONE',
        help='specify an alternative timezone for displaying timestamps (optional).',
    )
    rename_options_group = parser.add_argument_group('file rename options')
    rename_options_group.add_argument(
        '-o', '--outdir',
        action='store',
        metavar='OUT_DIR',
        help='move renamed files to this directory (can help avoid name conflicts during renaming).',
    )

    #
    # parse args, shared setup
    #

    args = parser.parse_args()

    dry_run = args.dry_run or bool(os.environ.get('DRY_RUN') or 0) # off by default
    if args.verbose: print("[Dry run: %s]" %dry_run)

    interactive = (args.interactive
        if args.interactive is not None
        else int(os.environ.get('PROMPT') or '1')
    ) # on by default
    if args.verbose: print("[Interactive: %s]" %interactive)

    paths = args.paths
    do_show_all = args.show
    do_check = args.check
    do_rename = args.rename
    #do_retag = args.retag

    outdir = args.outdir or os.environ.get('OUTPUT_DIR')
    if do_rename and outdir and not os.path.isdir(outdir):
        if os.path.exists(outdir):
            raise Exception('The specified output dir cannot be created because a file of the same name exists.')
        os.mkdir(outdir)
        if args.verbose: print('[Created output directory: %s]' %outdir)

    # load config files
    #cfgfile = cfgpath or os.path.join(os.environ['HOME'], '.%s'%_PROGRAM_NAME)
    cfgfile_device_names = os.path.join(args.cfgdir or os.environ['HOME'], '.%s_devinfo'%_PROGRAM_NAME)
    cfgfile_device_tzinfo = os.path.join(args.cfgdir or os.environ['HOME'], '.%s_tzinfo'%_PROGRAM_NAME)
    device_names = load_device_names(cfgfile_device_names)
    device_tzmap = load_device_tzinfo(cfgfile_device_tzinfo)
    if args.verbose:
        print('[Config file (device_names): %s]' %cfgfile_device_names)
        for key, item in device_names.items():
            print('\t%s:\t%s' %(key, item))
        print('[Config file (device_tzinfo): %s]' %cfgfile_device_tzinfo)
        for key, items in device_tzmap.items():
            print('\t%s:' %key)
            for item in items:
                print('\t\t%s' %str(item))

    # exif stores timestamps in local time and doesn't store timezone information
    # the original timezone needs to be supplied in order to standardize timestamps
    src_tzname = args.src_tz or os.environ.get('SRC_TZ')
    src_tz = get_timezone(src_tzname) if src_tzname else None
    if args.verbose: print('[Fallback timezone: %s]' %src_tzname)

    # also display timestamps in this timezone for convenience
    disp_tzname = args.disp_tz or os.environ.get('DISP_TZ')
    disp_tz = get_timezone(disp_tzname) if disp_tzname else None #get_timezone('UTC')

    #
    # process files
    #

    print("")
    good_paths = []
    error_paths = []
    skipped_paths = []

    for path in paths:
        basename = os.path.basename(path)
        dirname = os.path.dirname(path) or "."
        print("File %s (%s):" %(basename, dirname))
        if not os.path.isfile(path):
            print("\t(Error: Nonexistent file)")
            error_paths.append(path)
            continue

        #
        # get file information
        #
        imginfo = ImageInfo(path)
        if isinstance(imginfo, ImageInfo_unsupported):
            print('\t' + imginfo.msg())
            skipped_paths.append(path)
            continue

        prefix, nameinfo, modifier, ext, name_dt = print_parse_filename(basename, disp_tz=disp_tz)

        with imginfo:
            print("\tDimensions: %s" %str(imginfo.dimensions()))
            # TODO (later): also print file format, color & pixel format?

            if do_show_all:
                print_exif(imginfo)

            device_id = get_set_device_id_interactive(
                imginfo,
                cfgfile_device_names=cfgfile_device_names,
                interactive=interactive,
                device_names=device_names,
            )
            localtime, subsec_str = get_print_file_origin_timestamp(imginfo)
            if not localtime:
                print('\t(Skipping file due to missing metadata)')
                skipped_paths.append(path)
                continue

        #
        # timezone processing
        #
        if name_dt:
            print('\tLocalTimeInterpretation:FilenameTimeOffset: %s' %(
                (name_dt.replace(tzinfo=None) - localtime)
            ))

        def tz_cand_callback(device_tz, is_dst, dt, *, indent_level):
            #nonlocal name_dt
            #nonlocal disp_tz
            if disp_tz:
                print_timestamp_in(dt, disp_tz,
                    heading='DateTimeOriginal|DISP: ', indent_level=indent_level+1)
            if name_dt:
                print_timestamp_in(name_dt, device_tz,
                    heading='Filename:Timestamp|PRESET: ', indent_level=indent_level+1)
        tz_candidate = interpret_localtime_interactive(
            localtime,
            interactive=interactive,
            device_tzmap=device_tzmap,
            override_tz=src_tz,
            device_id=device_id,
            print_all=do_check,
            require_unique=(do_check or do_rename),
            cand_callback=tz_cand_callback,
        )
        if tz_candidate is None:
            print('\t(Skipping file)')
            skipped_paths.append(path)
            continue
        elif tz_candidate is False:
            print('\t(Error: unable to determine timezone - cannot proceed)')
            error_paths.append(path)
            continue
        else:
            _, _, dt = tz_candidate

        name_exif_mismatch = False
        if name_dt and (dt.timestamp() != name_dt.timestamp()):
            # TODO: ignore dropped precision?
            # TODO: if diverging by >= 30 minutes. mark as possible timezone error; otherwise treat as minor offset warning?
            print('\t(Warning: timestamp discrepancy detected between filename and EXIF in the specified timezone)')
            name_exif_mismatch = True

        #
        # main actions
        #
        error = False

        if do_check:
            if not name_dt:
                print("\t(File skipped)")
                skipped_paths.append(path)
                continue
            if name_exif_mismatch:
                print('\t(Check failed.)')
                error_paths.append(path)
                continue

        # TODO: prefix isn't currently included (ambiguity vs. modifier)
        if do_rename:
            if device_id is False and do_rename: # TODO: remove after updating modifier format
                print('\t(Skipping file due to missing configuration)')
                skipped_paths.append(path)
                continue
            if modifier is True and interactive:
                modifier = input_prefill('\t-> Modification detected, please enter a modifier abbreviation (clear to remove): ', '')
            elif modifier and interactive > 1:
                modifier = input_prefill('\t-> Modifier detected, please confirm (clear to remove): ', modifier)
            modifier_suffix = '_' + modifier if modifier else ''
            suggested_name = 'DSC%010i%s%s%s.%s' %(
                int(dt.timestamp()), # seconds under UTC
                ('.' + subsec_str) if subsec_str else '',
                ('_' + device_id) if device_id else '',
                modifier_suffix,
                ext
            )
            print('\tSuggested name: %s' %suggested_name)

            if dry_run:
                good_paths.append(path)
                continue

            outdirname = outdir or dirname
            error = not interactive_file_rename(suggested_name, outdirname, interactive=interactive)

        #elif do_retag:

        (error_paths if error else good_paths).append(path)

    print("")

    if len(error_paths):
        print("Errors:")
        for path in error_paths:
            print("\t%s" %path)

    if ((do_check or do_rename)# or do_retag)
        and len(good_paths)):
        print("Successfully processed:")
        for path in good_paths:
            print("\t%s" %path)

    if len(skipped_paths):
        print("Skipped:")
        for path in skipped_paths:
            print("\t%s" %path)
