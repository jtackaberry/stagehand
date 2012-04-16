"""
Crawl a tree and determine TV show names and episode codes from filenames.
"""
import sys, os, re


def get_epcode(name):
    season = episodes = None
    regexps = [
        # Note that some shows (e.g. Colbert Report) _do_ have 3-digit episode
        # numbers.

        # s01e02, s02e136, s01e02-03, s01e02-e04, s01e02e03
        (r's(\d{1,2})[\.-]?e(\d{1,3}(?:(?:-?e?\d{1,3})*\b))', 10, 10),
        # 1x02, 1x02x03, 1x02-03
        (r'(\d{1,2})x(\d{1,2}(?:(?:-?x?\d{1,2})*\b))', 9, 9),
        # foo.ep10, foo.ep10-11
        (r'\bep(\d+(?:-\d+)*)\b', 0, 6),
        # foo.s1.d2.e3, foo.s1.d2.e3-e4, foo.s1.d2.e3e4
        (r'\bs(\d+).*(e\d+(?:-?e?\d+)*)\b', 2, 4),
        # foo.e10, foo.e10-e11, foo.e10-11
        (r'\b(e\d+(?:-?e?\d+)*)\b', 0, 4),
        # foo.102
        (r'\b(\d{3,4})\b', 3, 3),
        # foo102
        (r'(\d{3,4})', 1, 1)
    ]

    # First dispense with some well-known red herrings.
    name = re.sub(r'x264|h\.?264|720p|1080p', '', name.lower(), re.I)

    for regexp, s_conf, e_conf in regexps:
        m = re.search(regexp, name, re.I)
        if not m:
            continue
        if len(m.groups()) == 1:
            code = m.group(1)
            if s_conf == 0:
                # Episode code only, no season.
                episodes = code.lstrip('e')
            else:
                # 3-4 digit season+episode code.
                if int(code) >= 1900 and int(code) <= 2050:
                    # Refuse to parse what looks to be a year into an episode code.
                    # Sorry, but series running longer than 18 years will have to
                    # be named properly. :)
                    continue
                episodes = code[-2:]
                season = code[:-2]
        else:
            season, episodes = m.groups()
        break

    if not episodes:
        return None, None, 0, 0

    season = int(season) if season else None
    episodes = [int(ep.strip('-xXeE')) for ep in re.split(r'[-xXeE]', episodes) if ep]
    if season == 0 and s_conf < 10:
        # Season 0 is probably bogus.   We only allow it for proper episode
        # codes s00e01.  For anything else, we drop the confidence levels since
        # the season and episode list are likely wrong.
        s_conf = e_conf = 0
    return season, episodes, s_conf, e_conf


def guess_episode_from_file(parts, name):
    name = name.replace('_', ' ')
    attrs = {
        'show': None,
        'season-dir': False,
        'season': None,
        'episodes': [],
        's-conf': -1,
        'e-conf': -1 
    }

    season, eps, s_conf, e_conf = get_epcode(name)
    if eps:
        attrs['season'] = season
        attrs['episodes'] = eps
        attrs['s-conf'] = s_conf
        attrs['e-conf'] = e_conf

    for part in reversed(parts):
        # See if this part contains a number or looks like an episode code.
        season, eps, s_conf, e_conf = get_epcode(part)
        if s_conf > attrs['s-conf'] and season:
            attrs['season'] = season
            attrs['s-conf'] = s_conf
        if e_conf > attrs['e-conf'] and eps:
            attrs['episodes'] = eps
            attrs['e-conf'] = e_conf

        # See if this directory part is a season directory.
        m = re.match(r'^(?:s|season|series)[_.\- ]*(\d{1,2})$', part, re.I)
        if m:
            # This is a pretty high confidence match for season directory name.
            season = int(m.group(1))
            if attrs['s-conf'] < 8:
                if attrs['season'] and attrs['season'] != season:
                    # This season doesn't match the one we previously determined,
                    # which now calls into question the validity of the episode
                    # list.  We need to drop the ep confidence.
                    attrs['e-conf'] = 0
                elif attrs['season'] == season:
                    # Season directory corroborates season parsed from episode
                    # code, which suggests episode list is accurate, so bump
                    # the episode confidence.
                    attrs['e-conf'] += 1
                attrs['season'] = season
                attrs['s-conf'] = 8
            attrs['season-dir'] = True
        elif part.isdigit() and len(part) <= 2:
            # Directory name is just a two digit number.  Could possibly be a
            # season number.  Set it if the current s-conf is 1 or less.
            if attrs['s-conf'] <= 1:
                attrs['season'] = season
                attrs['s-conf'] = 2
            attrs['season-dir'] = True

    # TOOD: split large single episode codes e.g. 010203

    if parts:
        attrs['show'] = parts[0]
    else:
        # No show, need to guess from filename
        attrs['show'] = 'TODO'
    return attrs


video_exts = ['mkv', 'avi', 'mpg', 'mpeg', 'mp4', 'm4v', 'm2ts', 'ts', 'mts', 'iso', 'vob']
prefix = os.path.realpath(sys.argv[1])
for dirpath, dirnames, filenames in os.walk(prefix, followlinks=True):
    parts = dirpath[len(prefix)+1:].split('/')
    n_zero_confidence = n_video_files = 0
    for fname in filenames:
        name, ext = os.path.splitext(fname)
        if ext[1:].lower() not in video_exts:
            continue
        attrs = guess_episode_from_file(parts, name)
        print '--', name
        print '      Show:', attrs['show']
        print '    Season:', attrs['season'], 'conf:', attrs['s-conf']
        print '  Episodes:', attrs['episodes'], 'conf:', attrs['e-conf']
        print '       Dir:', attrs['season-dir']
        n_video_files += 1
        if attrs['e-conf'] <= 0:
            n_zero_confidence += 1
    # If more than 30% of the files 
    if n_video_files and n_zero_confidence / float(n_video_files) > 0.3:
        print '<<<<<<<<<<<< Too many zero confidence files in this directory'
