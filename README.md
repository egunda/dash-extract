# DASH VOD URL Extractor

A Python script that parses MPEG-DASH VOD streams from a list of MPD manifest URLs and extracts every segment URL — initialization segments, media segments, and the MPD itself — into a flat text file ready for bulk download tools.

This is the DASH equivalent of the HLS extractor that walks Master → Variant playlists and collects `.ts` segment URLs.

---

## HLS vs DASH — Concept Mapping

| HLS | DASH |
|-----|------|
| Master `.m3u8` | MPD file (`.mpd`) |
| Variant `.m3u8` | `AdaptationSet > Representation` |
| `.ts` segments | Media segments (`.m4s`, `.mp4`, `.cmfv`, etc.) |
| `#EXT-X-MAP` init | Initialization segment (via `SegmentTemplate@initialization`) |
| `m3u8` Python library | `xml.etree.ElementTree` (stdlib) |

---

## Requirements

Python 3.7+ with the following packages:

```
requests
urllib3
```

Install with:

```bash
pip install requests urllib3
```

No third-party DASH library is needed — MPD files are standard XML parsed with Python's built-in `ElementTree`.

---

## File Structure

```
project/
├── dash_url_extractor.py   # Main script
├── input.txt               # Your list of MPD URLs (one per line)
├── all_dash_urls.txt       # Output: all extracted URLs (created on run)
└── progress.txt            # Run log (created on run)
```

---

## Usage

1. Create `input.txt` in the same directory as the script, with one MPD URL per line:

```
https://example.com/stream1/manifest.mpd
https://cdn.example.com/vod/stream2.mpd
https://media.example.com/content/abc/dash.mpd
```

2. Run the script:

```bash
python dash_url_extractor.py
```

3. Check the output:

- **`all_dash_urls.txt`** — all extracted URLs, one per line, prefixed with `TsvHttpData-1.0` for compatibility with bulk download tools
- **`progress.txt`** — per-MPD processing log with URL counts and any errors

---

## Output Format

```
TsvHttpData-1.0
https://example.com/stream1/manifest.mpd
https://example.com/stream1/video/init-stream0.mp4
https://example.com/stream1/video/chunk-stream0-00001.m4s
https://example.com/stream1/video/chunk-stream0-00002.m4s
...
https://example.com/stream1/audio/init-stream1.mp4
https://example.com/stream1/audio/chunk-stream1-00001.m4s
...
```

For each MPD, the output includes:
- The MPD URL itself
- All initialization segments (one per Representation)
- All media segments across every Period, AdaptationSet, and Representation

---

## Supported DASH Segment Addressing Modes

The script handles all three DASH segment addressing modes defined in ISO/IEC 23009-1:

### 1. SegmentTemplate + SegmentTimeline
The most common VOD pattern. Uses `<S t= d= r=>` entries to define explicit segment timing.

```xml
<SegmentTemplate timescale="90000" initialization="init-$RepresentationID$.mp4"
                 media="chunk-$RepresentationID$-$Number%05d$.m4s" startNumber="1">
  <SegmentTimeline>
    <S t="0" d="180000" r="59"/>
  </SegmentTimeline>
</SegmentTemplate>
```

### 2. SegmentTemplate with fixed `duration`
Computes total segment count from `mediaPresentationDuration` divided by segment duration.

```xml
<SegmentTemplate timescale="1000" duration="2000"
                 initialization="init.mp4" media="seg-$Number$.m4s"/>
```

### 3. SegmentList
Explicit list of `<SegmentURL>` elements per Representation.

```xml
<SegmentList>
  <Initialization sourceURL="init.mp4"/>
  <SegmentURL media="seg-001.m4s"/>
  <SegmentURL media="seg-002.m4s"/>
</SegmentList>
```

---

## Template Token Support

The script expands all standard DASH `SegmentTemplate` identifier tokens:

| Token | Description |
|-------|-------------|
| `$RepresentationID$` | The `id` attribute of the `<Representation>` element |
| `$Number$` | Segment number, starting from `startNumber` |
| `$Number%05d$` | Zero-padded segment number (any width) |
| `$Time$` | Segment start time in timescale units |
| `$Time%09d$` | Zero-padded time value (any width) |
| `$Bandwidth$` | The `bandwidth` attribute of the `<Representation>` element |

---

## BaseURL Inheritance

DASH allows `<BaseURL>` elements at any level of the hierarchy. The script correctly resolves relative paths through the full chain:

```
MPD BaseURL → Period BaseURL → AdaptationSet BaseURL → Representation BaseURL
```

Both absolute and relative `BaseURL` values are supported.

---

## Error Handling

- Network errors (connection timeout, HTTP errors) are caught per-MPD and logged; processing continues with the next URL
- XML parse errors are caught and logged without crashing the run
- SSL certificate verification is disabled to handle self-signed or misconfigured CDN certs (same behaviour as the HLS script)
- MPDs with no recognisable segment addressing fall back to `BaseURL`-only extraction

---

## Known Limitations

- **Live/dynamic streams** (`type="dynamic"` MPDs) are not supported — the script is designed for static VOD (`type="static"`) only
- **Byte-range segments** (segments addressed via `mediaRange` or HTTP byte ranges) are not extracted as individual URLs
- **Encrypted streams** (CENC, Widevine, PlayReady) — URLs are extracted normally; decryption keys are not handled
- **Multi-period content** is supported (all periods are iterated), but period-level `BaseURL` offsets are assumed to be URL-based, not time-based

---

