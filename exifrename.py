#!/usr/bin/env python3

import os
import os.path
import functools
print = functools.partial(print, flush=True)

def input_prefill(prompt, prefill=''):
    import readline
    readline.set_startup_hook(lambda: readline.insert_text(prefill))
    try:
        return input(prompt)
    finally:
        readline.set_startup_hook()

#
# image & exif definitions
#

USE_PILLOW = int(os.environ.get('USE_PILLOW') or '0')

if USE_PILLOW:
    import PIL.Image, PIL.ExifTags
    ExifTagValues = { v: k for k, v in PIL.ExifTags.TAGS.items() }
else:
    import pyexifinfo
EXIF_TIMESTAMP_FORMAT = '%Y:%m:%d %H:%M:%S'

def get_exif_value(exif, key_name):
    if USE_PILLOW:
        return exif.get(ExifTagValues[key_name]) # PIL
    else:
        return exif.get(key_name) # exiftool

def print_exif_value(exif, key_name):
    value = get_exif_value(exif, key_name)
    print('\t' + key_name + ': ' + str(value))
    return value

#
# devname map file handling
#

DEVNAME_FILE = os.path.join(os.environ['HOME'], '.exif_devnames')

def load_devname_map():
    name_map = {}
    if not os.path.isfile(DEVNAME_FILE):
        return {}
    with open(DEVNAME_FILE, mode='r') as file:
        for line in file:
            if not line or line[0] == '#': continue
            pair = line.split(',')
            name_map[pair[0].strip()] = pair[1].strip()
    return name_map

def save_devname_map(name_map):
    print('[Begin updating device name map]')
    with open(DEVNAME_FILE, mode='w') as file:
        for key, val in name_map.items():
            file.write(key + ',' + val + '\n')
    print('[Successfully updated device name map]')

#
# command-line parsing & environment setup
#

def load_paths():
    import sys
    return sys.argv[2:]
    #import glob
    #ifpattern = sys.argv[2]
    #return glob.glob(ifpattern)

def parse_method():
    import sys
    if not sys.argv[1] in ['show', 'rename', 'check']:
        raise ValueError('Invalid method')
    return sys.argv[1]

method = parse_method()

dry_run = int(os.environ.get('DRY_RUN') or '1') # dry-run is on by default
print("[Dry run: %s]" %bool(dry_run))

outdir = os.environ.get('OUTPUT_DIR')
if outdir and not os.path.isdir(outdir):
    if os.path.exists(outdir):
        raise Exception('The specified output dir cannot be created because a file of the same name exists.')
    os.mkdir(outdir)
    print('[Created output directory: %s]' %outdir)

show_prompt = int(os.environ.get('PROMPT') or '1') # prompt is on by default

devname_map = load_devname_map()
paths = load_paths()

# exif stores timestamps in local time and doesn't store timezone information
# the original timezone needs to be supplied in order to standardize timestamps
src_tzname = os.environ.get('SRC_TZ')
if not src_tzname and method != "check":
    raise ValueError('No timezone given')
# also display timestamps in this timezone for convenience
disp_tzname = os.environ.get('DISP_TZ')

from datetime import datetime, timezone
import pytz
src_timezone = pytz.timezone(src_tzname) if src_tzname else None
disp_timezone = pytz.timezone(disp_tzname) if disp_tzname else timezone.utc

#
# process files
#

good_paths = []
error_paths = []

for path in paths:
    basename = os.path.basename(path)
    dirname = os.path.dirname(path) or "."
    mainname = '.'.join(basename.split('.')[:-1])

    print("File %s (%s):" %(basename, dirname))

    if USE_PILLOW:
        img = PIL.Image.open(path)
        exif = img.getexif() # lacks HEIF support
    else:
        exif = pyexifinfo.information(path)
    if not exif:
        print("File has no EXIF data")
        continue

    is_new_naming_scheme = (
        basename.startswith("DSC")
        and basename[3:13].isdigit()
        and not basename[13].isdigit()
    ) #

    if is_new_naming_scheme:
        print("\t(File has new-style name)")
        name_dt = datetime.fromtimestamp(int(basename[3:13]), timezone.utc)
        name_parts = mainname.split("_")
        name_devname = name_parts[1]
        print("\tTimestamp|UTC: %s" %(name_dt.strftime(EXIF_TIMESTAMP_FORMAT)))
        print("\tTimestamp|DISP_TZ: %s" %(name_dt.astimezone(disp_timezone).strftime(EXIF_TIMESTAMP_FORMAT)))

    if method == 'check':
        if not is_new_naming_scheme:
            print("\tFile skipped")
            continue

    #
    # get file information
    # TODO: print resolution (_not_ EXIF)
    #

    if method == 'show':
        for key, val in exif.items():
            if USE_PILLOW: # PIL
                if key == ExifTagValues['MakerNote']:
                    print('MakerNote: (omitted)')
                    continue
                if key in PIL.ExifTags.TAGS:
                    print(f"\t{PIL.ExifTags.TAGS[key]}: {repr(val)}")
                elif key in PIL.ExifTags.GPSTAGS:
                    print(f"\t{PIL.ExifTags.GPSTAGS[key]}: {repr(val)}")
            else: # exiftool
                print(f"\t{key}: {str(val)}")
        continue

    if USE_PILLOW: # PIL
        #print_exif_value(exif, 'Make')
        model_str = print_exif_value(exif, 'Model')
        localtime_sec_str = print_exif_value(exif, 'DateTimeOriginal')
        subsec_str = print_exif_value(exif, 'SubsecTimeOriginal') or '' # might not exist
    else:
        model_str = print_exif_value(exif, 'EXIF:Model')
        localtime_sec_str = print_exif_value(exif, 'EXIF:DateTimeOriginal')
        subsec_str = print_exif_value(exif, 'EXIF:SubSecTimeOriginal') or '' # might not exist
        # NOTE:
        # * if subsec has leading zeroes, result is string
        # * if subsec has no leading zeroes, result is int
        if isinstance(subsec_str,int):
            subsec_str = str(subsec_str)

    if not model_str or not localtime_sec_str:
        print('\tSkipping file due to missing metadata')
        continue

    # get abbreviation of device name
    model_abbr = devname_map.get(model_str)
    if not model_abbr:
        model_abbr = input_prefill('Enter an abbreviation for this device model (%s):\n' %model_str)
        devname_map[model_str] = model_abbr
        save_devname_map(devname_map)
    else:
        print('\t<ModelAbbr>: %s' %model_abbr)

    if method == 'check' and not src_timezone:
        continue

    # process timestamp
    subsec_str = subsec_str[:1] # truncate to 1 digits if longer, too much precision is useless (we keep remaining digits to distinguish between images taken within the same second)
    localtime = datetime.strptime(localtime_sec_str, EXIF_TIMESTAMP_FORMAT)
    dt = src_timezone.localize(localtime)
    print('\tDateTimeOriginal|DISP_TZ: %s' %dt.astimezone(disp_timezone).strftime(EXIF_TIMESTAMP_FORMAT))
    utc_seconds = int(dt.timestamp())

    #
    # check name
    #
    if method == 'check':
        if dt.timestamp() != name_dt.timestamp():
            print('\t(Warning: name disagrees with EXIF - this may or may not be an error)')
            error_paths.append(path)
        else:
            good_paths.append(path)
        continue

    #
    # renaming
    #

    extsfx = basename.split('.')[-1]

    # extract "e" suffix or prefix from input name and append to output name

    if is_new_naming_scheme:
        name_suffix = name_parts[2] if len(name_parts) >= 3 else ''
        modifier_suffix = ("_" + name_suffix) if name_suffix else ''
        if modifier_suffix:
            print('\t(Detected "%s" suffix in new-style name)' %modifier_suffix)
    else:
        possible_suffix = False
        if extsfx == 'jpg':
            print('\t(Detected lower case file extension)')
            possible_suffix = True
        if mainname[-1] == 'e':
            print('\t(Detected trailing "e")')
            possible_suffix = '_e'
        parts = mainname.split('_')
        if len(parts) > 1:
            cand = parts[1]
            if cand[0] in 'eE' or cand[-1] in 'eE':
                print('\t(Detected "e" near counter value)')
                possible_suffix = '_e'
            elif cand[-1] in 'rR':
                print('\t(Detected "r" near counter value)')
                possible_suffix = '_r'

        if not possible_suffix:
            modifier_suffix = ''
        else:
            modifier_suffix = possible_suffix if isinstance(possible_suffix, str) else '_e'
    if modifier_suffix and show_prompt:
        modifier_suffix = input_prefill('\tModifier infix/suffix detected, please enter desired suffix: ', modifier_suffix)

    suggested_name = 'DSC%010i%s_%s%s.%s' %(utc_seconds, ('.' + subsec_str) if subsec_str else '', model_abbr, modifier_suffix, extsfx)

    print('\tSuggested name: %s' %suggested_name)
    if dry_run:
        continue

    import shutil
    while True:
        if show_prompt:
            accepted_name = input_prefill('\tSave to: ', suggested_name)
        else:
            accepted_name = suggested_name
        outdirname = outdir or dirname
        dstpath = os.path.join(outdirname, accepted_name)

        if (os.path.exists(dstpath)):
            print("Error: destination already exists")
            if show_prompt:
                continue
            else:
                error_paths.append(path)
                break

        try:
            shutil.move(path, dstpath)
        except shutil.Error as e:
            print(str(e))
            if show_prompt:
                continue
            else:
                error_paths.append(path)
                break
        break

if len(error_paths):
    print("Errors:")
for path in error_paths:
    print("\t%s" %path)

if len(good_paths): # not always printed, currently it will simply be omitted
    print("Successfully processed:")
for path in good_paths:
    print("\t%s" %path)
