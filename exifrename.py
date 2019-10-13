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

import PIL.Image, PIL.ExifTags
ExifTagValues = { v: k for k, v in PIL.ExifTags.TAGS.items() }
EXIF_TIMESTAMP_FORMAT = '%Y:%m:%d %H:%M:%S'

def get_exif_value(exif, key_name):
    return exif.get(ExifTagValues[key_name])

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
    if not sys.argv[1] in ['show', 'rename']:
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
src_tzname = os.environ['SRC_TZ']
if not src_tzname:
    raise ValueError('No timezone given')
# also display timestamps in this timezone for convenience
disp_tzname = os.environ.get('DISP_TZ') or 'UTC'

from datetime import datetime
import pytz
timezone = pytz.timezone(src_tzname)
disp_timezone = pytz.timezone(disp_tzname)

#
# process files
#

for path in paths:
    basename = os.path.basename(path)
    dirname = os.path.dirname(path)

    img = PIL.Image.open(path)
    exif = img.getexif()
    if not exif:
        print("File %s has no EXIF data" %path)
        continue
    # TODO: add HEIF support

    #
    # get file information
    # TODO: print resolution (_not_ EXIF)
    #

    if method == 'show':
        print("EXIF data for file %s:" %path)
        for key, val in exif.items():
            if key == ExifTagValues['MakerNote']:
                print('MakerNote: (omitted)')
                continue
            if key in PIL.ExifTags.TAGS:
                print(f"\t{PIL.ExifTags.TAGS[key]}: {repr(val)}")
            elif key in PIL.ExifTags.GPSTAGS:
                print(f"\t{PIL.ExifTags.GPSTAGS[key]}: {repr(val)}")
        continue

    print("File %s (%s):" %(basename, dirname))
    #print_exif_value(exif, 'Make')
    model_str = print_exif_value(exif, 'Model')
    localtime_sec_str = print_exif_value(exif, 'DateTimeOriginal')
    subsec_str = print_exif_value(exif, 'SubsecTimeOriginal') or '' # might not exist

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

    # process timestamp
    subsec_str = subsec_str[:1] # truncate to 1 digits if longer, too much precision is useless (we keep remaining digits to distinguish between images taken within the same second)
    localtime = datetime.strptime(localtime_sec_str, EXIF_TIMESTAMP_FORMAT)
    dt = timezone.localize(localtime)
    print('\tDateTimeOriginal_DISPTZ: %s' %dt.astimezone(disp_timezone).strftime(EXIF_TIMESTAMP_FORMAT))
    utc_seconds = int(dt.timestamp())

    #
    # renaming
    #

    extsfx = basename.split('.')[-1]

    # extract "e" suffix or prefix from input name and append to output name
    possible_suffix = False
    if extsfx == 'jpg':
        print('\t(Detected lower case file extension)')
        possible_suffix = True
    mainname = '.'.join(basename.split('.')[:-1])
    if mainname[-1] == 'e':
        print('\t(Detected trailing "e")')
        possible_suffix = True
    parts = mainname.split('_')
    if len(parts) > 1:
        cand = parts[1]
        if cand[0] in 'eE' or cand[-1] in 'eE':
            print('\t(Detected "e" near counter value)')
            possible_suffix = True
    if not possible_suffix:
        modifier_suffix = ''
    else:
        modifier_suffix = '_e'
        if show_prompt:
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
                break

        try:
            shutil.move(path, dstpath)
        except shutil.Error as e:
            print(str(e))
            if show_prompt:
                continue
            else:
                break
        break
