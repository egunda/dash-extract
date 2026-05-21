import os
import re
import requests
import urllib3
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# DASH XML namespace
DASH_NS = 'urn:mpeg:dash:schema:mpd:2011'
NS = {'mpd': DASH_NS}


def log_progress(message):
    with open("progress.txt", 'a') as progress_file:
        progress_file.write(message + '\n')
    print(message)


def get_attr(element, *attr_names):
    """Try multiple attribute names, return first match (handles namespaced vs plain)."""
    for name in attr_names:
        val = element.get(name)
        if val is not None:
            return val
    return None


def expand_segment_template(template, representation_id, bandwidth, number, time=None):
    """Replace DASH SegmentTemplate tokens with actual values."""
    result = template
    result = result.replace('$RepresentationID$', str(representation_id))
    result = result.replace('$Bandwidth$', str(bandwidth))

    # Handle $Number%0Nd$ style zero-padded format
    result = re.sub(
        r'\$Number%0(\d+)d\$',
        lambda m: str(number).zfill(int(m.group(1))),
        result
    )
    result = result.replace('$Number$', str(number))

    if time is not None:
        result = re.sub(
            r'\$Time%0(\d+)d\$',
            lambda m: str(time).zfill(int(m.group(1))),
            result
        )
        result = result.replace('$Time$', str(time))

    return result


def resolve_url(base_url, path):
    """Resolve a possibly-relative path against a base URL."""
    if path.startswith('http://') or path.startswith('https://'):
        return path
    return urljoin(base_url, path)


def get_base_url(element, parent_base_url):
    """Extract BaseURL from an element, falling back to parent."""
    base_url_el = element.find('mpd:BaseURL', NS)
    if base_url_el is None:
        base_url_el = element.find('BaseURL')  # without namespace
    if base_url_el is not None and base_url_el.text:
        return resolve_url(parent_base_url, base_url_el.text.strip())
    return parent_base_url


def extract_segments_from_representation(rep, period_base_url, adaptation_base_url, timescale_default=1):
    """
    Extract all segment URLs for a single Representation.
    Returns a list of absolute URLs.
    """
    segments = []
    rep_id = rep.get('id', '')
    bandwidth = rep.get('bandwidth', '0')
    rep_base_url = get_base_url(rep, adaptation_base_url)

    # --- SegmentTemplate (most common for VOD) ---
    # Check Representation-level first, then inherit from AdaptationSet parent
    seg_template = rep.find('mpd:SegmentTemplate', NS) or rep.find('SegmentTemplate')

    if seg_template is not None:
        timescale = int(seg_template.get('timescale', timescale_default) or timescale_default)
        initialization = seg_template.get('initialization')
        media_template = seg_template.get('media')
        start_number = int(seg_template.get('startNumber', 1))
        duration = seg_template.get('duration')

        # Initialization segment
        if initialization:
            init_url = resolve_url(rep_base_url, expand_segment_template(initialization, rep_id, bandwidth, 0))
            segments.append(init_url)

        # SegmentTimeline (explicit timing)
        timeline = seg_template.find('mpd:SegmentTimeline', NS) or seg_template.find('SegmentTimeline')
        if timeline is not None and media_template:
            t = 0
            number = start_number
            for s_el in (timeline.findall('mpd:S', NS) or timeline.findall('S')):
                t_attr = s_el.get('t')
                if t_attr is not None:
                    t = int(t_attr)
                d = int(s_el.get('d', 0))
                repeat = int(s_el.get('r', 0))
                for _ in range(repeat + 1):
                    seg_url = resolve_url(rep_base_url, expand_segment_template(media_template, rep_id, bandwidth, number, t))
                    segments.append(seg_url)
                    t += d
                    number += 1

        # Fixed duration (no timeline)
        elif duration and media_template:
            duration = float(duration)
            timescale = timescale or 1
            duration_sec = duration / timescale
            # Get period duration from parent — passed as kwarg below if available
            # Fallback: we'll just skip count-based (handled by caller passing period_duration)
            # Here we yield a generator approach: caller handles period_duration
            segments.append(('__template__', media_template, rep_id, bandwidth, start_number, rep_base_url))

        return segments

    # --- SegmentList ---
    seg_list = rep.find('mpd:SegmentList', NS) or rep.find('SegmentList')
    if seg_list is not None:
        init_el = seg_list.find('mpd:Initialization', NS) or seg_list.find('Initialization')
        if init_el is not None:
            src = init_el.get('sourceURL') or init_el.get('range', '')
            if src:
                segments.append(resolve_url(rep_base_url, src))

        for seg_url_el in (seg_list.findall('mpd:SegmentURL', NS) or seg_list.findall('SegmentURL')):
            media = seg_url_el.get('media')
            if media:
                segments.append(resolve_url(rep_base_url, media))
        return segments

    # --- BaseURL only (single-file representation) ---
    if rep_base_url and rep_base_url != adaptation_base_url:
        segments.append(rep_base_url)

    return segments


def parse_mpd(mpd_url, tree):
    """
    Parse an MPD ElementTree and return all segment URLs.
    Returns: (mpd_url, [all_segment_urls])
    """
    all_urls = [mpd_url]
    root = tree.getroot()

    # Strip namespace for easier tag matching if needed
    mpd_base_url = get_base_url(root, mpd_url)
    mpd_duration_str = root.get('mediaPresentationDuration', '')

    # Parse total duration (ISO 8601 like PT3600S or PT1H0M0S)
    total_duration_sec = None
    if mpd_duration_str:
        total_duration_sec = parse_iso8601_duration(mpd_duration_str)

    for period in (root.findall('mpd:Period', NS) or root.findall('Period')):
        period_base_url = get_base_url(period, mpd_base_url)
        period_duration_str = period.get('duration', '')
        period_duration_sec = parse_iso8601_duration(period_duration_str) if period_duration_str else total_duration_sec

        for adaptation in (period.findall('mpd:AdaptationSet', NS) or period.findall('AdaptationSet')):
            adaptation_base_url = get_base_url(adaptation, period_base_url)

            # Inherit SegmentTemplate from AdaptationSet if Representation doesn't have one
            adapt_seg_template = adaptation.find('mpd:SegmentTemplate', NS) or adaptation.find('SegmentTemplate')

            for rep in (adaptation.findall('mpd:Representation', NS) or adaptation.findall('Representation')):
                rep_id = rep.get('id', '')
                bandwidth = rep.get('bandwidth', '0')
                rep_base_url = get_base_url(rep, adaptation_base_url)

                # Use Representation-level template, or fall back to AdaptationSet-level
                seg_template = rep.find('mpd:SegmentTemplate', NS) or rep.find('SegmentTemplate') or adapt_seg_template

                if seg_template is not None:
                    timescale = int(seg_template.get('timescale', 1) or 1)
                    initialization = seg_template.get('initialization')
                    media_template = seg_template.get('media')
                    start_number = int(seg_template.get('startNumber', 1))
                    duration_ticks = seg_template.get('duration')

                    # Initialization segment
                    if initialization:
                        init_url = resolve_url(rep_base_url, expand_segment_template(initialization, rep_id, bandwidth, 0))
                        all_urls.append(init_url)

                    timeline = seg_template.find('mpd:SegmentTimeline', NS) or seg_template.find('SegmentTimeline')
                    if timeline is not None and media_template:
                        t = 0
                        number = start_number
                        for s_el in (timeline.findall('mpd:S', NS) or timeline.findall('S')):
                            t_attr = s_el.get('t')
                            if t_attr is not None:
                                t = int(t_attr)
                            d = int(s_el.get('d', 0))
                            repeat = int(s_el.get('r', 0))
                            for _ in range(repeat + 1):
                                seg_url = resolve_url(rep_base_url, expand_segment_template(media_template, rep_id, bandwidth, number, t))
                                all_urls.append(seg_url)
                                t += d
                                number += 1

                    elif duration_ticks and media_template and period_duration_sec:
                        duration_ticks = float(duration_ticks)
                        duration_sec = duration_ticks / timescale
                        total_segments = int(period_duration_sec / duration_sec)
                        for i in range(total_segments):
                            number = start_number + i
                            seg_url = resolve_url(rep_base_url, expand_segment_template(media_template, rep_id, bandwidth, number))
                            all_urls.append(seg_url)

                    continue  # Done with this representation

                # SegmentList
                seg_list = rep.find('mpd:SegmentList', NS) or rep.find('SegmentList')
                if seg_list is None:
                    seg_list = adaptation.find('mpd:SegmentList', NS) or adaptation.find('SegmentList')

                if seg_list is not None:
                    init_el = seg_list.find('mpd:Initialization', NS) or seg_list.find('Initialization')
                    if init_el is not None:
                        src = init_el.get('sourceURL')
                        if src:
                            all_urls.append(resolve_url(rep_base_url, src))
                    for seg_url_el in (seg_list.findall('mpd:SegmentURL', NS) or seg_list.findall('SegmentURL')):
                        media = seg_url_el.get('media')
                        if media:
                            all_urls.append(resolve_url(rep_base_url, media))
                    continue

                # Fallback: BaseURL-only single file
                if rep_base_url != adaptation_base_url:
                    all_urls.append(rep_base_url)

    return all_urls


def parse_iso8601_duration(duration_str):
    """Convert ISO 8601 duration string (e.g. PT1H30M0S, PT3600S) to seconds."""
    if not duration_str:
        return None
    pattern = re.compile(
        r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?'
        r'(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?'
    )
    m = pattern.match(duration_str)
    if not m:
        return None
    years, months, days, hours, minutes, seconds = m.groups(default='0')
    total = (
        float(years) * 365 * 86400 +
        float(months) * 30 * 86400 +
        float(days) * 86400 +
        float(hours) * 3600 +
        float(minutes) * 60 +
        float(seconds)
    )
    return total


def create_dash_url_file():
    input_file = 'input.txt'
    output_file = 'all_dash_urls.txt'

    # Clear output file
    open(output_file, 'w').close()

    with open(input_file, 'r') as infile:
        urls = [line.strip() for line in infile if line.strip()]

    with open(output_file, 'a') as outfile:
        outfile.write("TsvHttpData-1.0\n")

    for mpd_url in urls:
        log_progress(f'Processing MPD: {mpd_url}')
        try:
            response = requests.get(mpd_url, verify=False, timeout=30)
            response.raise_for_status()
        except Exception as e:
            log_progress(f"Error fetching MPD {mpd_url}: {e}")
            continue

        try:
            tree = ET.ElementTree(ET.fromstring(response.content))
        except ET.ParseError as e:
            log_progress(f"Error parsing MPD XML {mpd_url}: {e}")
            continue

        try:
            all_urls = parse_mpd(mpd_url, tree)
        except Exception as e:
            log_progress(f"Error processing MPD {mpd_url}: {e}")
            continue

        log_progress(f"  -> Found {len(all_urls)} URLs (including MPD itself)")
        with open(output_file, 'a') as outfile:
            for url in all_urls:
                outfile.write(f'{url}\n')

    log_progress("DASH URL extraction complete. Check 'all_dash_urls.txt' for the URLs.")


if __name__ == "__main__":
    create_dash_url_file()