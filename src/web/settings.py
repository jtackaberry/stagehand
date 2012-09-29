import os

def rename_example(tvdir, separator, season_dir_format, code_style, episode_format):
    show, title, season, ep = 'Dead Like Me', 'The Shallow End', 2, 4
    try:
        example = os.path.join(
            tvdir,
            season_dir_format.format(season=season),
            episode_format.format(
                show=show.replace(' ', separator),
                code=code_style.format(season=season, episode=ep),
                title=title.replace(' ', separator)
            )
        )
    except KeyError as (key,):
        return {'error': u'%s is not a valid identifier' % key}
    except ValueError as (err,):
        return {'error': err}
    else:
        return {'example': example}
