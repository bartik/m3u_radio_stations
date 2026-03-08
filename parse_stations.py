#!/usr/bin/env python3
"""Simple parser for SK_radio_stations.html and SK_radio_stations.array.

Produces a list of lightweight data objects extracted from the document.

- `fvb` entries get their numeric id from the onclick="tf(1234,this);" attribute.
- every other span in the station info block becomes an object where
  * `name` is the span's class value
  * `value` is the text content of the span

Usage:
    python parse_stations.py [html_path] [array_path] [--deaccent] [--prefix PREFIX] [--m3u [out_dir] | --extm3u [out_dir] | --iptv [out_dir]] [--onefile]

Options:
  html_path      Path to the HTML file (default SK_radio_stations.html)
  array_path     Path to the .array data file (default SK_radio_stations.array)
  --deaccent     Strip diacritics from titles and attributes
  --prefix PREFIX  Prefix to add to the filename of generated M3U files
  --m3u [out_dir]  Write one .m3u playlist per station. If out_dir is
                   omitted the current directory is used.
  --extm3u [out_dir]  Write one extended .m3u playlist per station. If out_dir is
                      omitted the current directory is used.
  --iptv [out_dir]  Write one IPTV extended .m3u playlist per station. If out_dir is
                    omitted the current directory is used.
  --onefile       Write all stations to a single M3U file instead of one per station.

If the array file is provided, stream information will be attached to each
station object under the `streams` key.  When `--m3u`, `--extm3u`, or `--iptv`
is specified, the JSON output is still printed but additional playlist files
are created. The --m3u, --extm3u, and --iptv options are mutually exclusive.
"""

import re
import json
import unicodedata
import argparse
import configparser
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict

from bs4 import BeautifulSoup


@dataclass
class StreamEntry:
    url: str
    format: Optional[str] = None
    bitrate: Optional[int] = None

    def normalized_url(self) -> str:
        """Return the URL with backslashes removed and a scheme added if missing."""
        u = self.url.replace("\\", "")
        if not re.match(r"^[a-zA-Z]+://", u):
            u = "https://" + u
        return u

    def to_dict(self):
        return {
            "url": self.url,
            "format": self.format,
            "bitrate": self.bitrate,
        }


@dataclass
class StationEntry:
    fvb: str          # required identifier, derived from the onclick attribute
    title: Optional[str]
    attributes: Dict[str, str]
    country_code: Optional[str] = None
    streams: Optional[List[StreamEntry]] = None
    fname: str = field(init=False)

    def __post_init__(self):
        base = self.title or self.fvb
        # remove accents
        nkfd = unicodedata.normalize("NFKD", base)
        base = "".join(ch for ch in nkfd if not unicodedata.combining(ch))
        # replace non-alphanumeric characters with _
        base = re.sub(r'[^A-Za-z0-9]', '_', base)
        # replace consecutive _
        base = re.sub(r'_+', '_', base)
        # strip leading/trailing _
        self.fname = base.strip('_')

    def to_dict(self):
        d = {"fvb": self.fvb, "title": self.title, "fname": self.fname, "country_code": self.country_code}
        d.update(self.attributes)
        if self.streams is not None:
            d["streams"] = [s.to_dict() for s in self.streams]
        return d

    def remove_accents(self):
        """Strip diacritics from title and attribute values in place."""
        def _strip(text: str) -> str:
            nkfd = unicodedata.normalize("NFKD", text)
            return "".join(ch for ch in nkfd if not unicodedata.combining(ch))

        if self.title:
            self.title = _strip(self.title)
        for k, v in list(self.attributes.items()):
            if isinstance(v, str):
                self.attributes[k] = _strip(v)


@dataclass
class Settings:
    html_path: Path
    array_path: Path
    deaccent: bool
    dir: Optional[Path]
    format: Optional[str]  # 'm3u', 'extm3u', 'iptv', or None
    prefix: str
    onefile: bool


def _sanitize_array_text(text: str) -> str:
    """Insert explicit None values for missing entries.

    The raw .array file often contains multiple consecutive commas to
    indicate an empty element, which is invalid syntax for
    ``ast.literal_eval`` (e.g. ``[a,,b]``).
    This helper rewrites such sequences so that every empty spot becomes
    ``None``.  It handles:

    * consecutive commas anywhere in the document
    * empty element at start ``[,``
    * empty element before closing bracket ``,]``
    """
    # repeatedly replace ``,,`` -> ``,None,`` until no more occur; the
    # loop handles runs of more than two empties.
    while ",," in text:
        text = text.replace(",,", ",None,")
    # fix up edge cases at the boundaries
    text = re.sub(r"\[,", "[None,", text)
    text = re.sub(r",\s*\]", ",None]", text)
    return text


def load_settings() -> Settings:
    parser = argparse.ArgumentParser(description="Parse station HTML and optional stream data.")
    parser.add_argument("html_path", nargs="?", default="SK_radio_stations.html",
                        help="path to HTML file")
    parser.add_argument("array_path", nargs="?", default="SK_radio_stations.array",
                        help="path to .array file")
    parser.add_argument("--deaccent", action="store_true",
                        help="strip diacritics from titles and attributes")
    parser.add_argument("--prefix", default="",
                        help="prefix to add to the filename of generated M3U files")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--m3u", nargs="?", const=".",
                       help="generate plain m3u playlists into directory")
    group.add_argument("--extm3u", nargs="?", const=".",
                       help="generate extended m3u playlists (with EXTINF) into directory")
    group.add_argument("--iptv", nargs="?", const=".",
                       help="generate IPTV extended m3u playlists into directory")
    parser.add_argument("--onefile", action="store_true",
                        help="Write all stations to a single M3U file instead of one per station")
    parser.add_argument("--config", "-c", help="INI config file with parameters")

    args = parser.parse_args()

    # apply config file defaults if present
    if args.config:
        cfg = configparser.ConfigParser()
        cfg.read(args.config)
        if "settings" in cfg:
            parser.set_defaults(**cfg["settings"])
            args = parser.parse_args()  # reparse with defaults

    # translate to Paths
    html_path = Path(args.html_path)
    array_path = Path(args.array_path)
    
    # Determine output directory and format
    if args.m3u is not None:
        out_dir = Path(args.m3u)
        out_format = 'm3u'
    elif args.extm3u is not None:
        out_dir = Path(args.extm3u)
        out_format = 'extm3u'
    elif args.iptv is not None:
        out_dir = Path(args.iptv)
        out_format = 'iptv'
    else:
        out_dir = None
        out_format = None

    return Settings(html_path=html_path,
                    array_path=array_path,
                    deaccent=args.deaccent,
                    dir=out_dir,
                    format=out_format,
                    prefix=args.prefix,
                    onefile=args.onefile)


def adjust_station_country(input_path: Path) -> Path:
    """Process the HTML file to convert Unicode Regional Indicator Symbols to HTML format.
    
    Reads the file character by character and converts sequences of Regional Indicator Symbols
    (used in flag emojis) to <h4 class="cntry">XX</h4> format where XX is the country code.
    
    Returns the path to a temporary file containing the processed content.
    """
    with input_path.open("r", encoding="utf-8") as f:
        content = f.read()
    
    # Process the content character by character
    result = []
    i = 0
    while i < len(content):
        char = content[i]
        # Check if this is a Regional Indicator Symbol (U+1F1E6 to U+1F1FF)
        if 0x1F1E6 <= ord(char) <= 0x1F1FF:
            # Look ahead for the second symbol
            if i + 1 < len(content):
                next_char = content[i + 1]
                if 0x1F1E6 <= ord(next_char) <= 0x1F1FF:
                    # Convert both symbols to letters
                    letter1 = chr(ord('A') + (ord(char) - 0x1F1E6))
                    letter2 = chr(ord('A') + (ord(next_char) - 0x1F1E6))
                    country_code = letter1 + letter2
                    # Replace with HTML format
                    result.append(f'<h4 class="cntry">{country_code}</h4>')
                    i += 2  # Skip both characters
                    continue
        # Not a regional indicator pair, add as-is
        result.append(char)
        i += 1
    
    processed_content = ''.join(result)
    
    # Write to a temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix='.html', text=True)
    with open(temp_fd, 'w', encoding='utf-8') as temp_file:
        temp_file.write(processed_content)
    
    return Path(temp_path)


def parse_station_entry(path: Path) -> List[StationEntry]:
    """Parse the given HTML file and return a list of Station objects.

    Skip any block that does not contain a valid `fvb` numeric id; the
    identifier is mandatory for downstream lookup.
    """
    with path.open("r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    stations: list[StationEntry] = []

    # each stnblock corresponds to a single station object
    for block in soup.select("div.stnblock"):
        fvb_id = None
        fvb = block.select_one("div.fvb")
        if fvb and fvb.has_attr("onclick"):
            onclick = fvb["onclick"]
            match = re.search(r"tf\((\d+),", onclick)
            if match:
                fvb_id = match.group(1)
        # if we didn't find an id, skip this block entirely
        if not fvb_id:
            continue

        title_elem = block.select_one("h3.stn")
        title = title_elem.get_text(strip=True) if title_elem else None

        attrs: dict = {}
        for span in block.select("div.stninfo span"):
            cls = span.get("class")
            if cls:
                key = cls[0]
                # if multiple spans have same class, concatenate
                text = span.get_text(strip=True)
                if key in attrs:
                    attrs[key] += ", " + text
                else:
                    attrs[key] = text

        # Extract country code from h4.cntry element
        country_elem = block.select_one("h4.cntry")
        country_code = country_elem.get_text(strip=True) if country_elem else None

        stations.append(StationEntry(fvb=fvb_id, title=title, attributes=attrs, country_code=country_code))

    return stations


def parse_stream_entry(path: Path) -> Dict[str, List[StreamEntry]]:
    """Read the .array file and produce a mapping from fvb id to stream list."""
    with path.open("r", encoding="utf-8") as f:
        raw = f.read().strip()

    clean = _sanitize_array_text(raw)

    # Find all inner lists using regex, disregarding nesting
    list_matches = re.findall(r'\[([^\[\]]*)\]', clean)

    streams_by_fvb: dict[str, list] = {}
    for match in list_matches:
        # Split by comma and parse each value
        values = [v.strip() for v in match.split(',')]
        if len(values) < 7:
            continue
        parsed_item = []
        for val in values:
            if val.startswith("'") and val.endswith("'"):
                parsed_item.append(val[1:-1])  # remove quotes
            elif val == 'None':
                parsed_item.append(None)
            elif val.isdigit():
                parsed_item.append(int(val))
            elif '.' in val and val.replace('.', '').replace('-', '').isdigit():
                parsed_item.append(float(val))
            else:
                parsed_item.append(val)
        
        # Now parsed_item is the list
        fvb = str(parsed_item[6]) if parsed_item[6] is not None else None
        entry = StreamEntry(
            url=parsed_item[0],
            format=parsed_item[1] if len(parsed_item) > 1 else None,
            bitrate=parsed_item[2] if len(parsed_item) > 2 else None,
        )
        streams_by_fvb.setdefault(fvb, []).append(entry)
    return streams_by_fvb


def write_station_m3u(f, st, fn):
    for entry in st.streams:
        f.write(entry.normalized_url() + "\n")


def write_station_extm3u(f, st, fn):
    for entry in st.streams:
        title = st.title or st.fvb
        f.write(f"#EXTINF:-1,{title} {entry.format} {entry.bitrate}\n")
        if "sty" in st.attributes:
            f.write(f"#EXTGENRE: {st.attributes['sty']}\n")
        f.write(entry.normalized_url() + "\n")


def write_station_iptv(f, st, fn):
    for entry in st.streams:
        title = st.title or st.fvb
        f.write(f'#EXTINF:-1 tvg-id="{st.fvb}" tvg-name="{title} {entry.format} {entry.bitrate}" tvg-logo="{fn}.png" tvg-country="{st.country_code}" group-title="radio" radio="true", {title}\n')
        f.write(entry.normalized_url() + "\n")


def write_files(stations: list, outdir: Path, prefix: str = "", onefile: bool = False, format: str = 'm3u'):
    """Create M3U files per station in *outdir* or a single file if onefile is True.

    The format can be 'm3u', 'extm3u', or 'iptv'.
    If onefile is True, write all to a single file '{prefix}radio_stations.m3u'.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    
    if format == 'm3u':
        writer = write_station_m3u
    elif format == 'extm3u':
        writer = write_station_extm3u
    elif format == 'iptv':
        writer = write_station_iptv
    else:
        raise ValueError(f"Unsupported format: {format}")
    
    if onefile:
        filepath = outdir / f"{prefix}radio_stations.m3u"
        with filepath.open("w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for st in stations:
                if not st.fvb or not st.streams:
                    continue
                fname = prefix + st.fname
                writer(f, st, fname)
    else:
        for st in stations:
            if not st.fvb or not st.streams:
                continue
            fname = prefix + st.fname
            filepath = outdir / f"{fname}.m3u"
            with filepath.open("w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                writer(f, st, fname)


def main(settings: Settings):
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # Process the HTML file to adjust country indicators
    adjusted_html_path = adjust_station_country(settings.html_path)
    
    stations = parse_station_entry(adjusted_html_path)
    try:
        streams_map = parse_stream_entry(settings.array_path)
    except FileNotFoundError:
        streams_map = {}

    for station in stations:
        if station.fvb in streams_map:
            station.streams = streams_map[station.fvb]
        if settings.deaccent:
            station.remove_accents()

    if settings.dir is None:
        output = [s.to_dict() for s in stations]
        logger.info(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        write_files(stations, settings.dir, settings.prefix, settings.onefile, settings.format)


if __name__ == "__main__":
    settings = load_settings()
    main(settings)
