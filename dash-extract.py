import re
import requests
import urllib3
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

NS = 'urn:mpeg:dash:schema:mpd:2011'


def Q(tag):
    """Clark-notation qualified tag: {urn:mpeg:dash:schema:mpd:2011}Tag"""
    return f'{{{NS}}}{tag}'


def find(el, tag):
    """
    Find a child element by tag. Uses Clark notation first (handles namespaced MPDs),
    falls back to bare tag (handles non-namespaced MPDs).
    IMPORTANT: always uses explicit `is not None` — never `or` on ET Elements,
    because a leaf Element with no children has bool() == False (Python ET quirk).
    """
    r = el.find(Q(tag))
    if r is not None:
        return r
    return el.find(tag)


def findall(el, tag):
    """findall equivalent of find() above."""
    r = el.findall(Q(tag))
    if r:
        return r
    return el.findall(tag)


def get_base_url(element, parent_base_url):
    """Extract <BaseURL> text from element and resolve against parent."""
    burl_el = find(element, 'BaseURL')
    if burl_el is not None and burl_el.text and burl_el.text.strip():
        href = burl_el.text.strip()
        if href.startswith('http://') or href.startswith('https://'):
            return href
        return urljoin(parent_base_url, href)
    return parent_base_url


def resolve(base, path):
    """Resolve a segment path against a base URL."""
    if not path:
        return base
    if path.startswith('http://') or path.startswith('https://'):
        return path
    return urljoin(base, path)


def expand(template, rep_id, bandwidth, number, time=None):
    """Expand DASH SegmentTemplate identifier tokens."""
    s = template
    s = s.replace('$RepresentationID$', str(rep_id))
    s = s.replace('$Bandwidth$', str(bandwidth))
    # Zero-padded number: $Number%05d$
    s = re.sub(r'\$Number%0(\d+)d\$', lambda m: str(number).zfill(int(m.group(1))), s)
    s = s.replace('$Number$', str(number))
    if time is not None:
        s = re.sub(r'\$Time%0(\d+)d\$', lambda m: str(time).zfill(int(m.group(1))), s)
        s = s.replace('$Time$', str(time))
    return s


def parse_iso8601_duration(s):
    """Convert ISO 8601 duration (PT0H9M54.00S, PT3600S, etc.) to seconds."""
    if not s:
        return None
    m = re.match(
        r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?'
        r'(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?', s
    )
    if not m:
        return None
    Y, Mo, D, H, Mi, S = (float(v or 0) for v in m.groups())
    return Y * 365 * 86400 + Mo * 30 * 86400 + D * 86400 + H * 3600 + Mi * 60 + S


def process_segment_template(st, rep_id, bw, base_url, period_dur_sec, out):
    """Extract all segment URLs from a SegmentTemplate element."""
    timescale  = float(st.get('timescale') or 1)
    init       = st.get('initialization')
    media      = st.get('media')
    start      = int(st.get('startNumber') or 1)
    dur_ticks  = st.get('duration')

    # Initialization segment
    if init is not None:
        out.append(resolve(base_url, expand(init, rep_id, bw, 0)))

    # SegmentTimeline — explicit segment list with timing
    tl = find(st, 'SegmentTimeline')
    if tl is not None and media is not None:
        t, number = 0, start
        for s_el in findall(tl, 'S'):
            t_attr = s_el.get('t')
            if t_attr is not None:
                t = int(t_attr)
            d      = int(s_el.get('d') or 0)
            repeat = int(s_el.get('r') or 0)
            for _ in range(repeat + 1):
                out.append(resolve(base_url, expand(media, rep_id, bw, number, t)))
                t += d
                number += 1
        return

    # Fixed duration — compute segment count from period duration
    if dur_ticks is not None and media is not None:
        if period_dur_sec is not None:
            seg_dur_sec = float(dur_ticks) / timescale
            n_segs = int(period_dur_sec / seg_dur_sec)
            for i in range(n_segs):
                out.append(resolve(base_url, expand(media, rep_id, bw, start + i)))
        else:
            log_progress(f"    WARNING: rep {rep_id} has fixed-duration SegmentTemplate "
                         f"but no period duration found — segments skipped")


def process_segment_list(sl, base_url, out):
    """Extract all segment URLs from a SegmentList element."""
    init_el = find(sl, 'Initialization')
    if init_el is not None:
        src = init_el.get('sourceURL')
        if src:
            out.append(resolve(base_url, src))
    for seg_el in findall(sl, 'SegmentURL'):
        media = seg_el.get('media')
        if media:
            out.append(resolve(base_url, media))


def parse_mpd(mpd_url, root):
    """
    Walk the MPD element tree and return a flat list of all URLs:
    [mpd_url, init_seg, seg1, seg2, ..., init_seg, seg1, ...]
    """
    out = [mpd_url]
    mpd_base = get_base_url(root, mpd_url)
    mpd_dur  = parse_iso8601_duration(root.get('mediaPresentationDuration'))

    for period in findall(root, 'Period'):
        p_base  = get_base_url(period, mpd_base)
        p_dur   = parse_iso8601_duration(period.get('duration'))
        eff_dur = p_dur if p_dur is not None else mpd_dur

        for adapt in findall(period, 'AdaptationSet'):
            a_base = get_base_url(adapt, p_base)
            # AdaptationSet-level SegmentTemplate/SegmentList — inherited by Representations
            a_st   = find(adapt, 'SegmentTemplate')
            a_sl   = find(adapt, 'SegmentList')

            for rep in findall(adapt, 'Representation'):
                rep_id = rep.get('id', '')
                bw     = rep.get('bandwidth', '0')
                r_base = get_base_url(rep, a_base)

                # Explicit `is not None` — critical: do NOT use `or` on ET Elements
                r_st = find(rep, 'SegmentTemplate')
                r_sl = find(rep, 'SegmentList')

                # Representation-level takes priority over AdaptationSet-level
                st = r_st if r_st is not None else a_st
                sl = r_sl if r_sl is not None else a_sl

                if st is not None:
                    process_segment_template(st, rep_id, bw, r_base, eff_dur, out)
                elif sl is not None:
                    process_segment_list(sl, r_base, out)
                elif r_base != a_base:
                    # BaseURL-only single-file representation
                    out.append(r_base)

    return out


def log_progress(message):
    with open("progress.txt", 'a') as f:
        f.write(message + '\n')
    print(message)


def create_dash_url_file():
    input_file  = 'input.txt'
    output_file = 'all_dash_urls.txt'

    open(output_file, 'w').close()
    open("progress.txt", 'w').close()

    with open(input_file, 'r') as f:
        urls = [line.strip() for line in f if line.strip()]

    with open(output_file, 'a') as f:
        f.write("TsvHttpData-1.0\n")

    for mpd_url in urls:
        log_progress(f'Processing MPD: {mpd_url}')
        try:
            response = requests.get(mpd_url, verify=False, timeout=30)
            response.raise_for_status()
        except Exception as e:
            log_progress(f"  ERROR fetching: {e}")
            continue

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as e:
            log_progress(f"  ERROR parsing XML: {e}")
            continue

        try:
            all_urls = parse_mpd(mpd_url, root)
        except Exception as e:
            log_progress(f"  ERROR processing: {e}")
            import traceback; traceback.print_exc()
            continue

        log_progress(f"  -> {len(all_urls)} URLs found (MPD + segments)")
        with open(output_file, 'a') as f:
            for url in all_urls:
                f.write(url + '\n')

    log_progress("Done. Check 'all_dash_urls.txt'.")


if __name__ == "__main__":
    create_dash_url_file()
