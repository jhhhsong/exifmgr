# exiflabeler

`exiflabeler` is an EXIF metadata manager that supports better handling for timestamp, timezone, device, and author metadata. It can be used to:
1. Manage device information using a config database, for one or multiple imagings device of interest (e.g. cameras, smartphones).
    * Devices are identified by the combination of manufacturer and model (and optionally author data), and associated with a name abbreviation
    * Devices can be associated with historical timezone data, so that EXIF timestamps can be interpreted correctly in the absence of the EXIF timezone tag.
2. Convert EXIF tags to filenames and vice versa (limited to the aforementioned tags), using data supplied through the config file and the command line to resolve missing/incorrect information.

Motivation:
* Most cameras and smartphones do not produce unambiguous filenames. Instead, they produce a serial number that typically loops around after 1,000 or 10,000 photographs taken. As a result, name collisions often occur when photographs taken across many years are stored into the same directory.
* This means it is often useful to name files chronologically; however:
    * Many cameras and smartphones record localtime without timezone information, making it difficult to reliably determine, at scale, the chronological order in which photographs were taken. This may require corrections in post-processing.
        * Specifically, ambiguities can be introduced (1) during daylight saving time transition, and (2) when changing the on-device timezone setting (sometimes performed automatically when location is changed, for example by Apple's iOS).
    * Most EXIF-manipulation tools are not natively timezone-aware, and correcting for timezone issues by hand can be error-prone.
        * For example, `exiftool`'s `-globalTimeShift` option can be used to adjust for timezone issues, but it must be specified manually in the command-line. This can be error-prone when multiple devices and multiple timezones are involved, especially if overlapping.
* To a lesser extent, filename ambiguities can also be caused by mixing photographs taken from multiple devices. This (as well as issues of convenience) calls for the use of an abbreviated device identifier.

## Usage

Command-line examples
```sh
# rename in interactive mode
exiflabeler.py rename -i /IMG_*.JPG

# rename files, interpreting timestamps as UTC-8 (equivalent methods shown below)
exiflabeler.py rename --src-tz=Pacific/Pitcairn ./IMG_*.JPG
exiflabeler.py rename --src-tz=UTC-0800 ./IMG_*.JPG
exiflabeler.py rename --src-tz=Etc/GMT+8 ./IMG_*.JPG

# check previously renamed files for correctness
exiflabeler.py check --src-tz=UTC-0800 ./*.JPG # with timezone override
exiflabeler.py check --disp-tz=America/New_York ./*.JPG # with additional timezone display for comparison
```

Sample config files: (see attached)

## License

GNU General Public License version 3

## Installing

Python 3.6 or newer is required.

Third-party package dependencies (python3):
* [`pytz`](https://pypi.org/project/pytz/) - required for timezone processing
* One or more of the following libraries providing EXIF support:
    * [`PIL` (aka. `pillow`)](https://pillow.readthedocs.io/en/stable/installation.html) [(GitHub)](https://github.com/python-pillow/Pillow)
        * lacks support for certain formats such as HEIF
        * cannot be used to write metadata (save function will recompress image resulting in loss of quality)
    * [`pyexiftool`](https://pypi.org/project/PyExifTool/) [(GitHub)](https://github.com/sylikc/pyexiftool)
        * requires `exiftool` to be installed ([instructions](https://exiftool.org/install.html))
        * supports most image formats
    * (*deprecated*) [`pyexifinfo`](https://pypi.org/project/pyexifinfo/) [(GitHub)](https://github.com/guinslym/pyexifinfo)
        * requires `exiftool` to be installed ([instructions](https://exiftool.org/install.html))
        * supports most image formats
        * noticeably slower (needs to start one process per file)
    * \> Listed in order of preference; if multiple are installed, the first library in the list that provides the required feature will be used.
