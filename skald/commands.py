"""Preview and apply one personal change, shared by search, CLI and API."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import textwrap
import uuid
def _op(group, verb=None, inverse=None, *, feedback=None, kids=False, removes=False, hint=''):
    return dict(group=group, verb=verb, inverse=inverse, feedback=feedback, kids=kids, removes=removes, hint=hint)
# One table per op: parsing, prompt, preview, apply and undo read it instead of their own lists.
REGISTRY = {
    'hide': _op('prefs', inverse='show'), 'show': _op('prefs', inverse='hide', removes=True),
    'exclude_genre': _op('prefs', 'No more', 'include_genre'), 'include_genre': _op('prefs', 'More', 'exclude_genre', removes=True),
    'focus': _op('prefs', 'More', 'unfocus'), 'unfocus': _op('prefs', 'Less', 'focus', removes=True),
    'discovery': _op('prefs', 'Discovery:'), 'years': _op('prefs'),
    'like': _op('feedback', feedback='love'), 'dislike': _op('feedback', feedback='not_for_us'), 'seen': _op('feedback', feedback='seen'),
    'less': _op('prefs'), 'more': _op('prefs'), 'runtime': _op('prefs'), 'kids_age': _op('actor'),
    'settings': _op('read', kids=True), 'history': _op('read', kids=True), 'undo': _op('history', kids=True),
    'redo': _op('history', kids=True), 'reset': _op('reset', kids=True), 'shelf_list': _op('read', kids=True),
    'shelf_add': _op('shelf', kids=True), 'shelf_stop': _op('shelf', kids=True), 'shelf_count': _op('shelf', kids=True),
    'row_rename': _op('rows'), 'row_hide': _op('rows'), 'row_show': _op('rows'), 'row_move': _op('rows'),
    'plan': _op('plan', hint='plan is one sentence with several changes, kind null values [] steps [{op,kind,values,person}] in spoken order; steps use the change ops above.'),
    'correct': _op('correct', hint='correct fixes the change just made: "no, I meant Ben" person ben values []; "not horror, thriller" values [horror,thriller]. Defer, reject, already seen and why questions are not correct.'),
    'ask': _op('ask', kids=True, hint='ask is a requested change with an unclear detail, kind null values [] person null, slot change/person/title/scope/date/schedule, question one short question: "Mach es für sie weniger gruselig" slot person; "Maybe hide some of those?" slot change.'),
    'wish': _op('wish', kids=True, hint='wish is a hope (I wish, ich wünschte, volela bih, it would be nice), values [X], person only if named: "I wish there were more gentle mysteries" values [gentle mysteries]. Direct requests like more comedy are changes.'),
    'shelf_filter': _op('rule', kids=True, hint='shelf_filter values [shelf name], filters {kind,runtime,years,languages,exclude_genres,hide} with only stated keys.'),
    'shelf_schedule': _op('rule', kids=True, hint='shelf_schedule is a shelf in some months only, values [theme], months [1..12].'),
    'member_create': _op('member', hint='member_create adds a household member: "Add a household member named Kim, age 12" person kim age 12.'),
    'member_update': _op('member', hint='member_update person existing member, age or label: "Update Ben to age 13" person ben age 13; "Ändere Anas Anzeigenamen zu Kim" person ana label Kim. Titles like New Member are op null.'),
    'search': _op('search', kids=True, hint='search changes nothing: kind similar for something like X, people for movies by X, mood for something cozy tonight; values [X]. '
                  'kind title for a title name that reads like a mood, setting or command: "More Like Home" values [More Like Home]; "Film Mirno" values [Mirno]. '
                  'Fix plain title typos: somthing like Arrivl is similar [Arrival]. Other searches stay op null.'),
    'defer': _op('title', hint='defer hides one title from one person\'s picks for a while, never forever: values [title], until tonight or YYYY-MM-DD as stated: '
                 '"Not tonight for Dune" until tonight; "Arrival bitte nicht vor dem 2026-12-24 zeigen" until 2026-12-24. No clear date is ask slot date.'),
    'reject': _op('title', hint='reject is "not for me" about one title: values [title], reason the stated reason text or null: "Dune nije za mene, previše je spor" reason previše je spor. '
                  '"X is already seen" or "X habe ich schon gesehen" is seen, not reject.'),
    'ignore_learning': _op('title', hint='ignore_learning stops one watched title shaping this person\'s taste; history stays: values [title]: "Lerne nichts aus meinem Abspielen von Dune".'),
    'reattribute': _op('title', hint='reattribute moves a watch to the member who really watched it: values [title], person that member: "Arrival je gledao Ben, ne ja" person ben.'),
    'explain': _op('title', kids=True, hint='explain is a why question about one title in the picks and changes nothing: "Why is Dune here?" values [Dune].'),
    'like_results': _op('results', feedback='love', hint='like_results saves every shown result as the actor\'s taste: "I like these, save them as my taste". Use only with shown results context; kind null values [] person null.'),
    'shelf_from_results': _op('results', kids=True, hint='shelf_from_results saves the shown results as a row or shelf named from the last search: "save this as a row". Use only with shown results context; kind null values [] person null.'),
}
OPS = tuple(REGISTRY)
BASE = ('op', 'kind', 'values', 'person')
STEP = {'op': (*OPS, None), 'kind': ('movie', 'tv', 'series', 'genre', 'people', 'languages', 'countries', 'similar', 'mood', 'title', None),
        'values': ([(str, int, None), 10], None), 'person': (str, None)}
SLOTS = {'change': 'Which change do you mean?', 'person': 'Who should this apply to?', 'title': 'Which title do you mean?',
         'scope': 'Should this apply to you or to everyone?', 'date': 'Until which date?', 'schedule': 'Which days and hours should this shelf show?'}
FILTERS = {'kind': ('movie', 'series', None), 'runtime': (int, None), 'years': ([(int, None), 2], None),
           'languages': ([str, 10], None), 'exclude_genres': ([str, 10], None), 'hide': ([str, 5], None)}
OUT = {**STEP, 'steps': ([STEP, 6], None), 'slot': (*SLOTS, None), 'question': (str, None), 'age': (int, None),
       'label': (str, None), 'months': ([int, 12], None), 'filters': (FILTERS, None), 'until': (str, None), 'reason': (str, None)}
NONE = dict(op=None, kind=None, values=[], person=None)

def parse_json(content):
    """Decode the first balanced object, preferring an explicit JSON fence."""
    fence = re.search(r'```json\s*(.*?)```', content, re.S | re.I)
    content = fence[1] if fence else content
    start = content.find('{')
    depth, quoted, escaped = 0, False, False
    for i in range(start, len(content)) if start >= 0 else ():
        char = content[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                answer = json.loads(content[start:i+1])
                return {**dict.fromkeys(BASE), **answer}
    raise json.JSONDecodeError('No balanced JSON object', content, max(start, 0))

# Small phrase tables for local rules; the model covers everything else.
LOOKUP = re.compile(r'(?:find|search for|look for|suche|such|finde|pronađi|nađi|tražim|traži|пронађи|нађи|тражим)\s+(?:me\s+|mir\s+)?'
                    r'(?:the\s+|a\s+|den\s+|die\s+|das\s+|einen\s+)?(?:film|movie|series|serie|show|serija|serije|филм|серију)\b.*', re.I)
HIDES = {'anime': 'anime', 'аниме': 'anime', 'documentaries': 'documentaries', 'dokus': 'documentaries', 'animation': 'animation',
         'movies': 'movies', 'series': 'series'}
GENRES = {'horror': 'Horror', 'horor': 'Horror', 'хорор': 'Horror', 'comedies': 'Comedy', 'komödien': 'Comedy', 'komedija': 'Comedy',
          'комедија': 'Comedy', 'documentaries': 'Documentary', 'dokus': 'Documentary', 'reality-tv': 'Reality',
          'thriller': 'Thriller', 'thrillers': 'Thriller', 'triler': 'Thriller', 'trilera': 'Thriller'}
TYPES = {'movies': 'movie', 'filme': 'movie', 'filmova': 'movie', 'филмова': 'movie',
         'series': 'series', 'serien': 'series', 'serija': 'series', 'серија': 'series'}
LANGS = {'german': 'de', 'deutsch': 'de', 'nemačkom': 'de', 'nemački': 'de', 'english': 'en', 'englisch': 'en', 'engleskom': 'en',
         'french': 'fr', 'französisch': 'fr', 'francuskom': 'fr', 'serbian': 'sr', 'serbisch': 'sr', 'srpskom': 'sr'}
MONTHS = [m.split() for m in (
    'january januar januara јануар јануара', 'february februar februara фебруар фебруара', 'march märz mart marta март марта',
    'april aprila април априла', 'may mai maj maja мај маја', 'june juni jun juna јун јуна', 'july juli jul jula јул јула',
    'august avgust avgusta август августа', 'september septembar septembra септембар септембра',
    'october oktober oktobar oktobra октобар октобра', 'november novembar novembra новембар новембра',
    'december dezember decembar decembra децембар децембра')]
MONTH = {name: i for i, names in enumerate(MONTHS, 1) for name in names}
WHO = {'me': None, 'mich': None, 'mene': None, 'мене': None, 'us': 'together', 'uns': 'together', 'nas': 'together',
       'нас': 'together', 'together': 'together', 'zusammen': 'together', 'everyone': 'together'}
ASKS = (('scope', r'.*\b(?:for|für|za)\b.*\b(?:or maybe|oder vielleicht|ili možda)\s+(?:for|für|za)\b.*'),
        ('change', r'(?:maybe |vielleicht |možda )?(?:hide|show|remove|sakrij|blende)\s+(?:some of |a few of |einige von )?(?:those|these|them|that|it|die|das|to)\??'),
        ('person', r'.*\b(?:für (?:sie|ihn)|for (?:her|him)|za nju|za njega)\b.*'),
        ('title', r'.*\b(?:maybe|vielleicht|možda|можда)\b.*\b(?:watched|seen|gesehen|gledal[ao]|гледал[ао])\b.*'),
        ('date', r'.*\b(?:until|bis|do|до)\s+(?:sometime|someday|irgendwann|nekad|некад)\b.*'),
        ('schedule', r".*\b(?:not sure|don't know|weiß nicht|ne znam|не знам)\b.*\b(?:days|tage|dane|дане)\b.*"))

def who_word(s, word):
    """A person word maps to a member, together or me; False when it is not one."""
    key = word.casefold()
    if key in WHO:
        return WHO[key]
    hit = member_name(s, word) if s is not None else None
    return hit if s is not None and hit in s.PEOPLE else False

def split_person(raw, s):
    match = re.fullmatch(r'(?:for|für|za|за)\s+(\w+)[,:]?\s+(.+)', raw, re.I)
    if match and who_word(s, match[1]) is not False:
        return match[2], who_word(s, match[1])
    match = re.fullmatch(r'(.+?),?\s+(?:for|für|za|за)\s+(\w+)[.!]?', raw, re.I)
    if match and who_word(s, match[2]) is not False:
        return match[1], who_word(s, match[2])
    return raw, None

def parse_one(raw, s, steps=False):
    """One change without a person: the old local rules, plus short phrases inside a plan."""
    q = raw.casefold().rstrip('.!')
    op, kind, values = None, None, []
    hides = {**HIDES, **{h: h for h in getattr(s, 'HIDE_CHOICES', ())}}
    if match := re.fullmatch(r'rename (.+?) to (.+)', raw, re.I):
        op, values = 'row_rename', [row_id(match[1]), match[2]]
    elif match := re.fullmatch(r'call (.+?) row (.+)', raw, re.I):
        op, values = 'row_rename', [row_id(match[1]), match[2]]
    elif match := re.fullmatch(r'(?:hide|remove) (.+)|(.+?) (?:reihe )?ausblenden', q):
        name = match[1] or match[2]
        key = row_id(name)
        if key and name != 'anime':
            op, values = 'row_hide', [key]
    elif match := re.fullmatch(r'(?:show|bring back) (.+)', q):
        key = row_id(match[1])
        if key and match[1] != 'anime':
            op, values = 'row_show', [key]
    elif (match := re.fullmatch(r'add (?:the )?(.+?) (?:row|reihe)', q)) and row_id(match[1]) not in (None, 'anime'):
        op, values = 'row_show', [row_id(match[1])]
    elif match := re.fullmatch(r'(?:put|move) (.+?) (?:above|before) (.+)', q):
        op, values = 'row_move', [row_id(match[1]), row_id(match[2])]
    if op:
        return dict(NONE) if any(v is None for v in values) else dict(op=op, kind=None, values=values, person=None)
    if q in ('undo', 'rückgängig'):
        op = 'undo'
    elif match := re.fullmatch(r'(undo|rückgängig|bring back|redo|do again|wiederholen|nochmal) (.+)', q):
        op, values = ('redo' if match[1] in ('redo', 'do again', 'wiederholen', 'nochmal') else 'undo'), [match[2]]
    elif match := re.fullmatch(r'reset (filters|shelves)|reset (everything)(?: to default)?|(filter|regale|alles)(?: auf (?:standard|standardwerte))? zurücksetzen', q):
        choice = next(v for v in match.groups() if v)
        op, values = 'reset', [{'filter': 'filters', 'regale': 'shelves', 'alles': 'everything'}.get(choice, choice)]
    elif q in ('what did i change', 'history', 'verlauf', 'was habe ich geändert'):
        op = 'history'
    elif q in ('settings', 'my settings', 'what are my settings'):
        op = 'settings'
    elif q in ('show my shelves', 'list shelves', 'shelf list'):
        op = 'shelf_list'
    elif match := re.fullmatch(r'kids age (\d+)', q):
        op, values = 'kids_age', [int(match[1])]
    elif match := re.fullmatch(r'(?:hide|no) horror(?: (movies|series))?', q):
        op, kind, values = 'exclude_genre', {'movies': 'movie', 'series': 'series', None: None}[match[1]], ['Horror']
    elif match := re.fullmatch(r'no (movies|series) longer than (\d+) (minutes|hours)', q):
        op, kind, values = 'runtime', 'movie' if match[1] == 'movies' else 'series', [int(match[2]) * (60 if match[3] == 'hours' else 1)]
    elif match := re.fullmatch(r'(less|more|weniger) (horror|documentaries|comedies|reality-tv)', q):
        op, kind = 'less' if match[1] == 'weniger' else match[1], 'genre'
        values = [{'horror': 'Horror', 'documentaries': 'Documentary', 'comedies': 'Comedy', 'reality-tv': 'Reality'}[match[2]]]
    elif q == 'fewer series please':
        op, kind = 'less', 'series'
    elif match := re.fullmatch(r'I already watched (.+)', raw, re.I):
        op, values = 'seen', [match[1]]
    if op or not steps:
        pass
    elif match := re.fullmatch(r'(hide|sakrij|сакриј|show|zeige) (\S+)|(\S+) (wieder anzeigen|ausblenden)', q):
        verb, name = (match[1], match[2]) if match[1] else (match[4], match[3])
        if name in hides:
            op, values = 'show' if verb in ('show', 'zeige', 'wieder anzeigen') else 'hide', [hides[name]]
    elif match := re.fullmatch(r'(?:cap|limit) (movies|series|filme|serien) (?:at|to|auf) (\d+) (?:minutes|minuten|min)', q):
        op, kind, values = 'runtime', TYPES[match[1]], [int(match[2])]
    elif match := re.fullmatch(r'(?:keinen?|kein|bez) (\S+)', q):
        if match[1] in GENRES:
            op, values = 'exclude_genre', [GENRES[match[1]]]
    elif match := re.fullmatch(r'(?:only|nur) (?:movies|filme) (?:from|since|after|ab|seit) (\d{4})(?: on)?', q):
        op, values = 'years', [int(match[1]), None]
    elif match := re.fullmatch(r'(less|fewer|more|weniger|mehr|manje|više|мање|више) (\S+)', q):
        word = 'less' if match[1] in ('less', 'fewer', 'weniger', 'manje', 'мање') else 'more'
        if match[2] in GENRES:
            op, kind, values = word, 'genre', [GENRES[match[2]]]
        elif match[2] in TYPES:
            op, kind = word, TYPES[match[2]]
    elif match := re.fullmatch(r'(?:den )?fokus auf (\w+) entfernen|(?:remove the focus on|stop focusing on) (\w+)', q):
        if (match[1] or match[2]) in LANGS:
            op, kind, values = 'unfocus', 'languages', [LANGS[match[1] or match[2]]]
    elif re.search(r'(?:mix|mischung|mešavin|мешавин)', q) and re.search(r'(?:popular|populär|popularn|популарн)', q):
        op, values = 'discovery', ['balanced']
    return dict(op=op, kind=kind, values=values, person=None) if op else dict(NONE)

def parse_plan(raw, s):
    """Split on and/und/i/и only when every part is a change on its own."""
    def change(text):
        answer = parse_one(text, s, steps=True)
        return answer if REGISTRY.get(answer['op'], {}).get('group') in ('prefs', 'rows') else None
    for match in re.finditer(r',?\s+(?:and|und|i|и)\s+', raw, re.I):
        first = change(raw[:match.start()])
        rest = raw[match.end():]
        tail = parse_plan(rest, s) or ([change(rest)] if change(rest) else None) if first else None
        if tail:
            return [first, *tail]
    return None

def parse_ask(q):
    slot = next((slot for slot, pattern in ASKS if re.fullmatch(pattern, q)), None)
    return dict(op='ask', slot=slot, question=SLOTS[slot]) if slot else None

def parse_search(raw):
    # A title after film/movie starts with a capital, so "film noir tonight" stays a mood search for the model.
    if match := re.fullmatch(r'(?:(?i:the|der|den|das|a)\s+)?(?i:film|movie|филм)\s+["„“]?([A-ZÀ-ÖØ-ÞА-ЯЂЈЉЊЋЏČĆŠŽĐ0-9][^"„“”]*?)["“”]?[.!?]?', raw):
        return dict(op='search', kind='title', values=[match[1]])
    if match := re.fullmatch(r'(?:something|anything|movies?|films?|shows?|series|mehr|etwas|nešto|нешто|filmove)\s+(?:like|wie|kao|као|poput)\s+(?!(?:that|this|it|das|dies|to|ovo|то|ово)\W*$)(.+?)(?:,?\s+(?:bitte|please|molim))?[.!?]?', raw, re.I):
        return dict(op='search', kind='similar', values=[match[1]])
    if match := re.fullmatch(r'(?:movies|films|shows|series|filme|serien|filmovi)\s+(?:by|from|von|od)\s+(.+?)(?:\s+(?:for tonight|tonight|für heute|heute|za večeras|večeras))?', raw, re.I):
        return dict(op='search', kind='people', values=[match[1]])
    if match := re.fullmatch(r'(?:(?:heute|tonight|večeras|вечерас)\s+)?(?:etwas|was|something|nešto|нешто)\s+(\w+)(\s+(?:for tonight|tonight|heute|za večeras|за вечерас))?(?:,?\s+(?:bitte|please|molim|молим))?[.!?]?', raw, re.I):
        if match[2] or re.match(r'heute|tonight|večeras|вечерас', raw, re.I):
            return dict(op='search', kind='mood', values=[match[1]])
    return None

# Title fixes: one title, one person. Each pattern names the title group; dates are ISO or DD.MM.YYYY.
DATE = r'(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})'
TONIGHT = r'(?:not tonight|nicht heute abend|heute abend nicht|heute nicht|ne večeras|ne veceras|не вечерас)'
DEFERS = (TONIGHT + r'\s*(?:for|für|za|за|:|,|-)?\s*(.+)', r'(.+?),?\s+' + TONIGHT,
          r'(?:hide|skip|snooze)\s+(.+?)\s+(?:until|till|through)\s+' + DATE,
          r"(?:don't|do not)\s+show\s+(?:me\s+)?(.+?)\s+(?:before|until)\s+" + DATE,
          r'(?:blende|verstecke)\s+(.+?)\s+bis\s+(?:zum\s+)?' + DATE + r'(?:\s+aus)?',
          r'(.+?)\s+(?:bitte\s+)?(?:erst\s+)?nicht\s+vor\s+(?:dem\s+)?' + DATE + r'(?:\s+(?:zeigen|anzeigen))?',
          r'(?:ne prikazuj|не приказуј|sakrij|сакриј)\s+(.+?)\s+(?:pre|пре|do|до)\s+' + DATE)
REJECT = (r"(.+?)\s+(?:is not for me|isn't for me|is not my thing|ist nichts für mich|ist nicht für mich|nije za mene|није за мене)"
          r'(?:(?:\s*[,:;-]\s*|\s+(?:because|weil|jer|јер)\s+)(.+))?')
SEEN = (r'(.+?)\s+(?:is already seen|was already seen|habe ich (?:schon|bereits) gesehen|hab ich (?:schon|bereits) gesehen|'
        r'kenne ich schon|sam već gledala|sam već gledao|сам већ гледала|сам већ гледао)',
        r"(?:i(?:'ve| have)? already (?:watched|seen|saw)|već sam (?:gledala|gledao)|vec sam (?:gledala|gledao)|већ сам (?:гледала|гледао))\s+(.+)",
        r'(?:ich habe|hab)\s+(.+?)\s+(?:schon|bereits)\s+gesehen')
IGNORES = (r"(?:don't|do not|please don't)\s+learn\s+(?:anything\s+)?from\s+(?:my|me)\s+(?:watching|viewing|playing|plays?|watch)\s+(?:of\s+)?(.+)",
           r"(?:don't|do not)\s+let\s+(.+?)\s+(?:shape|change|affect)\s+my\s+taste",
           r'(?:lerne?|lernt)\s+(?:bitte\s+)?nichts\s+(?:aus|von)\s+(?:meinem|meinen)\s+\w+\s+(?:von\s+)?(.+)',
           r'(?:ne uči|nemoj (?:da )?učiš|не учи)\s+(?:ništa\s+|ништа\s+)?(?:iz|od|из|од)\s+(?:mog|mojeg|мог)\s+\w+\s+(.+)')
MOVES = (r'(\w+)\s+(?:watched|saw)\s+(.+?),?\s+not\s+(?:me|i)', r'(.+?)\s+(?:je gledao|je gledala|је гледао|је гледала)\s+(.+?),?\s+(?:ne ja|не ја)',
         r'(\w+)\s+hat\s+(.+?)\s+(?:geschaut|gesehen|angeschaut),?\s+nicht ich', r'(.+?)\s+hat\s+(\w+)\s+(?:geschaut|gesehen|angeschaut),?\s+nicht ich')
EXPLAINS = (r'why (?:is|was) (.+?) (?:here|there|in (?:my|our|the) \w+|recommended|suggested|showing|shown)\b.*',
            r'why (?:do|did) you (?:recommend|suggest|show) (?:me )?(.+?)(?: to me| for me)?',
            r'warum (?:wird|wurde) mir (.+?) (?:empfohlen|vorgeschlagen|gezeigt)', r'warum (?:ist|steht) (.+?) (?:hier|da|in meinen \w+)\b.*',
            r'warum empfiehlst du (?:mir )?(.+)', r'zašto je (.+?) (?:ovde|ovdje|tu|u mojim \w+|preporučen\w*)\b.*',
            r'zašto mi (?:preporučuješ|predlažeš) (.+)', r'зашто је (.+?) (?:овде|ту)\b.*')
NOT_TITLES = {'it', 'this', 'that', 'this one', 'that one', 'them', 'es', 'das', 'den', 'ihn', 'to', 'ovo', 'taj', 'то', 'ово', 'me', 'mich', 'ja', 'i'}

def fix_title(text, s):
    """A title, never a pronoun, a member name, a genre or a whole type."""
    title = text.strip().strip('"\'„“”«»').strip()
    key = title.casefold()
    if not title or len(title) > 100 or key in NOT_TITLES or key in GENRES or key in TYPES or key in HIDES:
        return None
    if s is not None and who_word(s, title) is not False:
        return None
    return title

def parse_fix(raw, s):
    head = raw.split(';')[0].strip().rstrip('.!?').strip()
    rest, person = split_person(head, s) if s is not None else (head, None)
    if person not in getattr(s, 'PEOPLE', ()):
        return parse_title_fix(head, s)
    found = parse_title_fix(rest, s)
    return {**found, 'person': person} if found and person is not None and found['op'] != 'reattribute' else found

def parse_title_fix(head, s):
    for pattern in DEFERS:
        if match := re.fullmatch(pattern, head, re.I):
            title, when = match[1], match[2] if match.lastindex == 2 else 'tonight'
            if (day := re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', when)):
                when = f'{day[3]}-{int(day[2]):02d}-{int(day[1]):02d}'
            title = fix_title(title, s)
            return dict(op='defer', values=[title], until=when) if title else None
    if match := re.fullmatch(REJECT, head, re.I):
        title = fix_title(match[1], s)
        return dict(op='reject', values=[title], reason=(match[2] or '').strip()[:200] or None) if title else None
    for op, patterns in (('seen', SEEN), ('ignore_learning', IGNORES), ('explain', EXPLAINS)):
        for pattern in patterns:
            if match := re.fullmatch(pattern, head, re.I):
                title = fix_title(match[1], s)
                return dict(op=op, values=[title]) if title else None
    for pattern in MOVES:
        if match := re.fullmatch(pattern, head, re.I):
            first, second = match[1], match[2]
            for who, title in ((first, second), (second, first)):
                person = who_word(s, who) if s is not None else False
                if person and person != 'together' and fix_title(title, s):
                    return dict(op='reattribute', values=[fix_title(title, s)], person=person)
            return None
    return None

def parse_wish(raw, s):
    patterns = (r'i wish (?:there (?:were|was) |i had |we had )?(.+)', r'it would be nice if (\w+) had (.+)', r'it would be nice to have (.+)',
                r'ich wünschte,? es gäbe (.+)', r'ich wünsche mir (.+)', r'schön wäre (.+)', r'(?:volela|voleo|волела|волео) (?:bih|бих) (.+)')
    for pattern in patterns:
        match = re.fullmatch(pattern, raw.rstrip('.!'), re.I)
        if match:
            break
    else:
        return None
    text, person = split_person(match[match.lastindex], s)
    if match.lastindex == 2:
        person = who_word(s, match[1]) or None
    text = re.sub(r'\s+(?:in|on) (?:my|our) rows$|\s+in meinen reihen$', '', text, flags=re.I)
    text = re.sub(r'^(?:more|mehr|više|више|some|eine größere auswahl(?: an)?|a bigger choice of)\s+', '', text, flags=re.I)
    return dict(op='wish', values=[text], person=person)

def parse_member(raw, s):
    q = raw.casefold().rstrip('.!')
    create = (r'add (?:a )?(?:new )?(?:household )?member(?: named| called)? (\w+)(?:,? (?:age|aged) (\d+)|,? (\d+) years old)?',
              r'neues mitglied (\w+)(?:,? (\d+) jahre alt)?(?:,? bitte anlegen|,? anlegen)?',
              r'dodaj (?:novog )?člana (\w+)(?:,? ima (\d+) godin[ae])?')
    for pattern in create:
        if match := re.fullmatch(pattern, q):
            age = next((int(v) for v in match.groups()[1:] if v), None)
            return dict(op='member_create', person=match[1], age=age)
    if match := re.fullmatch(r'update (\w+) to age (\d+)|(\w+) is now (\d+)(?: years old)?|(\w+) sada ima (\d+) godin\w*(?:,.*)?', q):
        name, age = [v for v in match.groups() if v]
        return dict(op='member_update', person=member_name(s, name).casefold() if s else name, age=int(age))
    if match := re.fullmatch(r"ändere (\w+) anzeigenamen (?:zu|in|auf) (\S+)|change (\w+)'s (?:display )?name to (\S+)", raw.rstrip('.!'), re.I):
        name, label = [v for v in match.groups() if v]
        return dict(op='member_update', person=member_name(s, name).casefold() if s else name.casefold(), label=label)
    return None

def parse_correct(raw, s):
    if match := re.fullmatch(r'(?:(?:no|nein|ne),?\s+)?(?:i meant|i ment|ich meinte|mislio sam|mislila sam)(?:\s+(?:na|for|für|za))?\s+(?!to\s)(.+?)[.!]?', raw, re.I):
        who = who_word(s, match[1]) if s is not None else match[1].casefold()
        return dict(op='correct', person=who) if who else dict(op='correct', values=[match[1]])
    if match := re.fullmatch(r'(?:not|nicht|ne)\s+([^,;]+?)(?:,\s*(?:but\s+|sondern\s+|nego\s+|već\s+)?|\s+(?:but|sondern|nego|već)\s+)([^,;]+?)[.!]?', raw, re.I):
        return dict(op='correct', values=[match[1], match[2]])
    return None

def shelf_filters(text):
    q = text.casefold()
    filters = {}
    if re.search(r'\b(?:movies?|films?|filme|filmovi|filmove|филмови)\b', q):
        filters['kind'] = 'movie'
    elif re.search(r'\b(?:series|serien\w*|serij\w*|episodes|серије)\b', q):
        filters['kind'] = 'series'
    if match := re.search(r'\b(?:under|unter|bis|up to|do|ispod)\s+(\d+)\s*(?:minutes|minuten|min|minuta)\b', q):
        filters['runtime'] = int(match[1])
    if match := re.search(r'\b(?:from|von|od)\s+(\d{4})\s+(?:to|bis|do)\s+(\d{4})\b', q):
        filters['years'] = [int(match[1]), int(match[2])]
    if match := re.search(r'\b(?:in|auf|na)\s+(\w+)$', q):
        if match[1] in LANGS:
            filters['languages'] = [LANGS[match[1]]]
    if match := re.search(r'\b(?:exclude|without|no|ohne|keine?n?|isključi|bez)\s+(\S+)', q):
        if re.fullmatch(r'movies?|films?|filme|filmov[ia]|series|serien|serij\w*', match[1]):  # A whole type keeps the other.
            filters['kind'] = 'movie' if re.match(r'ser', match[1]) else 'series'
        elif match[1] in HIDES:
            filters['hide'] = [HIDES[match[1]]]
        elif match[1] in GENRES:
            filters['exclude_genres'] = [GENRES[match[1]]]
    return filters

def parse_shelf_rule(raw, s):
    text, person = split_person(raw.rstrip('.!'), s)
    patterns = (r'limit (?:my |the |our )?(.+?) shelf to (.+)', r'(?:in|on) (?:my |the |our )?(.+?) shelf,? (.+)',
                r'(exclude .+?) from (?:my |the |our )?(.+?) shelf', r'im regal (\S+),? (.+)', r'(?:na|u) polici (\S+),? (.+)',
                r'(?:на|у) полици (\S+),? (.+)')
    for i, pattern in enumerate(patterns):
        if match := re.fullmatch(pattern, text, re.I):
            name, rest = (match[2], match[1]) if i == 2 else (match[1], match[2])
            filters = shelf_filters(rest)
            if filters:
                return dict(op='shelf_filter', values=[name], person=person, filters=filters)
    names = '|'.join(sorted(MONTH, key=len, reverse=True))
    months = r'((?:' + names + r')(?:(?:\s*,\s*|\s+(?:and|und|i|и)\s+)(?:' + names + r'))*)'
    patterns = (r'(.+?) every ' + months, r'every ' + months + r' (.+)', r'(?:jeden|jedes jahr im|im) ' + months + r' (.+)',
                r'(.+?) jeden ' + months, r'(.+?) (?:svakog|сваког) ' + months, r'(?:svakog|сваког) ' + months + r' (.+)')
    for pattern in patterns:
        if match := re.fullmatch(pattern, text, re.I):
            theme, when = (match[2], match[1]) if match[1].casefold().split()[0] in MONTH else (match[1], match[2])
            found = [MONTH[m] for m in re.split(r'\s*,\s*|\s+(?:and|und|i|и)\s+', when.casefold())]
            return dict(op='shelf_schedule', values=[theme], person=person, months=sorted(set(found)))
    return None

def parse_local(text, s=None, viewer=None, bound=None):
    raw = text.strip()
    if LOOKUP.fullmatch(raw):
        return dict(NONE)
    about = parse_about(raw, s)
    if about is not False:
        return dict(NONE, op='settings', person=about)
    found = parse_ask(raw.casefold()) or parse_search(raw) or parse_fix(raw, s) or parse_wish(raw, s) or parse_member(raw, s) or parse_correct(raw, s) or parse_shelf_rule(raw, s)
    if found:
        return {**NONE, **found}
    answer = parse_shelf(text, s, viewer, bound)
    if answer['op']:
        return answer
    person = None
    match = re.fullmatch(r'(.+?) (?:for|für) (\w+)', raw, re.I)
    if match and match[2].casefold() != 'you':
        raw, person = match.groups()
        person = None if person.casefold() == 'me' else 'together' if person.casefold() in ('us', 'together', 'uns', 'zusammen') else member_name(s, person) if s else person.casefold()
    head, who = split_person(raw, s) if s is not None and person is None else (raw, person)
    steps = parse_plan(head, s)
    if steps:
        return dict(op='plan', kind=None, values=[], person=None, steps=[{**x, 'person': who} for x in steps])
    answer = parse_one(raw, s)
    return {**answer, 'person': person} if answer['op'] else dict(NONE)

def row_id(name):
    from .members import ROW_KEYS
    name = re.sub(r'^(?:the )| (?:row|reihe)$', '', name.strip().casefold()).strip()
    aliases = {'top 10': 'top10', 'top rated': 'toprated', 'top rated movies': 'toprated',
               'latest digital releases': 'digital', 'for you': 'foryou', 'new to you': 'newtoyou',
               'continue watching': 'episodes', 'continue watching & new episodes': 'episodes'}
    return aliases.get(name, name if name in ROW_KEYS else None)

def row_settings(cfg):
    return {key: copy.deepcopy(cfg['household'].get(key)) for key in ('rows', 'row_labels', 'row_order')}

def edit_rows(cfg, change, s):
    from .members import ROW_KEYS
    op, kind, values = change['op'], change['kind'], change['values']
    count = 2 if op in ('row_rename', 'row_move') else 1
    if kind is not None or len(values) != count or values[0] not in ROW_KEYS:
        raise ValueError('row id or values')
    rows = list(cfg['household'].get('rows', [key for key in ROW_KEYS if key not in ('digital', 'toprated', 'newtoyou')]))
    if op == 'row_rename':
        label = values[1]
        if not isinstance(label, str) or not label.strip() or len(label) > 100 or any(ord(c) < 32 for c in label):
            raise ValueError('row label')
        cfg['household'].setdefault('row_labels', {})[values[0]] = label.strip()
        return
    if op == 'row_hide':
        rows = [key for key in rows if key != values[0]]
    elif op == 'row_show':
        if values[0] not in rows:
            rows.append(values[0])
    else:
        if values[1] not in rows or values[0] not in rows or values[0] == values[1]:
            raise ValueError('move needs two different visible rows')
        rank = {cid: i for i, cid in enumerate(s.DEDUPE_ORDER)}
        groups = s.row_groups()
        order = list(cfg['household'].get('row_order', sorted(ROW_KEYS,
            key=lambda key: min((rank.get(cid, len(rank)) for cid in groups.get(key, [s.rid(key)])), default=len(rank)))))
        order.extend(key for key in ROW_KEYS if key not in order)
        order.remove(values[0])
        order.insert(order.index(values[1]), values[0])
        cfg['household']['row_order'] = order
        return
    cfg['household']['rows'] = rows
def member_name(s, name):
    """Also accept Serbian accusative (Anu, Bena) and German genitive (Anas)."""
    key = name.casefold()
    keys = [key] + ([key[:-1], key[:-1]+'a'] if key[-1:] == 'u' else [key[:-1]] if key[-1:] in ('a', 's') else []) * (len(key) >= 3)
    return next((p for k in keys for p in s.PEOPLE if k in (p.casefold(), s.LABELS[p].casefold())), name)

ABOUT = re.compile(r'(?:what do you (?:know|knwo) about|was wei(?:ß|ss)t du über|[šs]ta zna[šs] o) (.+?)[.!?]?', re.I)

def parse_about(query, s):
    match = ABOUT.fullmatch(query.strip())
    if not match:
        return False
    if match[1].casefold() in ('me', 'mich', 'mene'):
        return None
    named = member_name(s, match[1]) if s is not None else None
    return named if named in getattr(s, 'PEOPLE', ()) else False

def shelf_hits(s, name, scope):
    from . import shelves
    alias = re.fullmatch(r'the (.+) shelf', name.casefold())
    key = alias[1] if alias else name.casefold()
    return [w for w in shelves.read(s)['wishes']
            if (scope not in (*s.PEOPLE, 'together') or w['scope'] in (scope, 'together'))
            and (w['name'].casefold() == key or alias and key == 'sunday'
                 and w['when'] != 'always' and w['when']['days'] == ['sun'])]

def shelf_schedule(q):
    from .shelves import DAYS
    days = dict(zip(('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'), DAYS))
    days.update(dict(zip(('montags', 'dienstags', 'mittwochs', 'donnerstags', 'freitags', 'samstags', 'sonntags'), DAYS)))
    days.update({d: d for d in DAYS})
    days.update(dict.fromkeys(('weekday', 'weekdays', 'wochentags', 'unter der woche'), 'weekdays'))
    days.update(dict.fromkeys(('weekend', 'am wochenende'), 'weekend'))
    timing = {'morning': '05-12', 'mornings': '05-12', 'morgens': '05-12',
              'afternoon': '12-18', 'nachmittags': '12-18', 'evening': '18-24', 'evenings': '18-24', 'abends': '18-24'}
    day_pattern = '|'.join(re.escape(d) for d in sorted(days, key=len, reverse=True))
    match = re.fullmatch(r'(?:every )?((?:'+day_pattern+r')(?:\s*(?:,|\band\b|\bund\b)\s*(?:'+day_pattern+r'))*)\s+(.+)', q)
    if not match:
        return None
    day_text, rest = match.groups()
    day_list = [days[d.strip()] for d in re.split(r'\s*(?:,|\band\b|\bund\b)\s*', day_text)]
    if len(day_list) > 1 and any(d not in DAYS for d in day_list):
        return None
    match = re.fullmatch(r'('+ '|'.join(timing) +r')\s+(.+)', rest)
    if match:
        hours, theme = timing[match[1]], match[2]
    else:
        match = re.fullmatch(r'(?:at |um )?(\d{1,2})(?::00)?(?:\s*(?:-|to|bis)\s*(\d{1,2})(?::00)?)?(?: Uhr| uhr)?\s+(.+)', rest)
        if not match:
            return None
        start, end, theme = match.groups()
        # An explicit start without an end lasts until midnight.
        hours = start+'-'+(end or '24')
        theme = re.sub(r'^(?:a|an|the)\s+', '', theme)
    theme = re.sub(r'^(?:einen?|eine)\s+', '', theme)
    theme = {'klassiker': 'classic', 'was kurzes': 'something short'}.get(theme, theme)
    return [theme, ','.join(dict.fromkeys(day_list)), hours]

def parse_shelf(text, s=None, viewer=None, bound=None):
    raw = text.strip()
    q = raw.casefold()
    creation = (r'(?:(?:create|make|add|new)\s+(?:me\s+)?(?:(?:a|an)\s+)?(?:new\s+)?'
                r'(?:catalog|row|shelf|list)\s+(?:about|for|with|of)|'
                r'(?:erstelle|erstell|mache|mach|füge)\s+(?:mir\s+)?(?:(?:ein|eine|einen)\s+)?'
                r'(?:neue[nrms]?\s+)?(?:katalog|reihe|regal|liste)\s+(?:über|für|mit|von)|'
                r'neue[nrms]?\s+(?:katalog|reihe|regal|liste)\s+(?:über|für|mit|von))\s+(.+)')
    person = None
    names = {'me': None}
    if s is not None:
        names.update({name.casefold(): p for p in s.PEOPLE for name in (p, s.LABELS[p])})
    names.update(dict.fromkeys(('us', 'both', 'both of us', 'everyone', 'together', 'uns', 'beide', 'zusammen', 'alle'), 'together'))
    for name in sorted(names, key=len, reverse=True):
        suffix = next((suffix for suffix in (' for '+name, ' für '+name) if q.endswith(suffix)), None)
        if suffix:
            person, q = names[name], q[:-len(suffix)]
            raw = raw[:-len(suffix)]
            break
    else:
        match = re.search(r' for (\w+)$', q)
        if match and not re.fullmatch(creation, raw, re.I):
            person, q = match[1], q[:match.start()]
            raw = raw[:match.start()]
    values, op = [], None
    schedule = shelf_schedule(q)
    if schedule:
        values, op = schedule, 'shelf_add'
    elif match := re.fullmatch(creation, raw, re.I):
        values, op = [match[1], 'always'], 'shelf_add'
    elif re.fullmatch(r'show \d+ shelves', q):
        values, op = [int(q.split()[1])], 'shelf_count'
    elif q == 'fewer shelves':
        values, op = ['fewer'], 'shelf_count'
    elif q.startswith('stop ') and s is not None:
        scope = person or (bound if bound in s.PEOPLE else viewer)
        if len(shelf_hits(s, q[5:], scope)) == 1:
            values, op = [q[5:]], 'shelf_stop'
    return dict(op=op, kind=None, values=values, person=person) if op else dict(NONE)

def shelf_scope(s, named, viewer, bound):
    actor = bound if bound in s.PEOPLE else viewer
    target = named or actor
    if target != 'together':
        target = next((p for p in s.PEOPLE if str(target).casefold() in (p, s.LABELS[p].casefold())), target)
    if actor == s.GUEST or bound == s.GUEST:
        raise PermissionError('Ask a grown-up to change settings')
    if target not in (*s.PEOPLE, 'together'):
        raise ValueError('Add a member name to this command')
    if s.kids_age(actor) is not None and target != actor:
        raise PermissionError('Ask a grown-up to change other shelves')
    if bound in s.PEOPLE and target not in (bound, 'together'):
        raise PermissionError('This plan applies to another member')
    return target

def shelf_preview(s, change, viewer, bound):
    from . import shelves
    scope = shelf_scope(s, change['person'], viewer, bound)
    op, values = change['op'][6:], change['values']
    kwargs = {}
    if change['kind'] is not None or op == 'add' and (len(values) not in (2, 3) or not all(isinstance(v, str) for v in values)) or op in ('stop', 'count') and len(values) != 1:
        raise ValueError('invalid shelf command')
    if op == 'add':
        if len(values) != (2 if values[1] == 'always' else 3):
            raise ValueError('invalid shelf schedule')
        text = values[0]
        when = 'always' if values[1] == 'always' else values[1]+' '+values[2]
        theme = re.sub(r'^(?:a|an|the|some)\s+', '', text, flags=re.I).title()
        day = 'Sunday ' if values[1] == 'sun' else 'Weekday ' if values[1] == 'weekdays' else ''
        name = day + theme
        kwargs = dict(text=text, name=name, when=when)
        if len([w for w in shelves.read(s)['wishes'] if w['scope'] == scope and w['slot'] <= shelves.slot_count(s, scope)]) >= shelves.slot_count(s, scope):
            raise ValueError('Shelves are full; stop a shelf first')
    elif op == 'stop':
        if not isinstance(values[0], str):
            raise ValueError('invalid shelf name')
        name = values[0]
        hits = shelf_hits(s, name, scope)
        if len(hits) != 1:
            raise ValueError('unknown or ambiguous shelf name')
        wish = hits[0]
        scope = shelf_scope(s, wish['scope'], viewer, bound)
        kwargs = dict(slot=wish['slot'], when=wish['when'])
        name = wish['name']
    else:
        count = max(0, shelves.slot_count(s, scope)-1) if values[0] == 'fewer' else values[0]
        kwargs = dict(count=count)
        name = str(count)+' shelves'
    shelf = s.shelf_change(scope, op, **kwargs)
    when = shelf['when']
    timing = 'always on' if when == 'always' else ('weekdays' if when['days'] == shelves.DAYS[:5] else ','.join(when['days']))+' '+str(when['hours'][0])+'-'+str(when['hours'][1])+' '+s.CONFIG['region']['tz']
    label = 'Together' if scope == 'together' else s.LABELS[scope]
    title = 'Add '+day+'shelf: '+theme if op == 'add' else op.capitalize()+' '+name
    return dict(people=[scope], shelf=shelf, plan=shelf['plan'], name=title+' for '+label+'; '+timing)

def command_prompt(s):
    return ('Return JSON with every key always present: op, kind, values, person; null when unknown. Exactly one persistent personal change, one extra op below or op null. '
              'Do not guess person, kind, days or hours unless clearly stated. Title names, questions and ordinary one-off searches return op null, except explicit settings/list requests. '
              'Never choose hide/show for a genre: hide horror movies means exclude_genre Horror kind movie. '
              'Genre exclusion or inclusion without a type uses kind null and applies to movies and series. '
              'Only hide movies/series when asked to remove the whole type. Explicit shelf requests are changes. Ops: ' + ', '.join(OPS) + '. '
              'hide/show values: ' + ', '.join(s.HIDE_CHOICES) + '. Genre values use TMDB genre names; '
              'kind movie or series. focus/unfocus kind languages or countries uses two-letter codes, '
              'or movie/series uses genres. discovery values: balanced/mainstream/gems. years: [from,to], null ends allowed. '
              'focus/unfocus kind people uses explicitly named actors or directors. like/dislike/seen values: one title, kind movie or tv or null. person: member name, everyone or null. '
              'less/more kind genre uses genre names, or movie/series with empty values for a soft type weight, never a ban. '
              'runtime kind movie/series values [maximum minutes]; kids_age kind null values [age 3..17]. '
              'settings, shelf_list and history kind null values []. History includes what did I change and Verlauf. '
              'undo kind null values [] for the latest change or [thing] for a named past change; bring back horror is undo [horror]. '
              'redo / do again kind null values [thing] replay a past change. '
              'reset kind null values [filters], [shelves] or [everything]; reset everything to default and alles auf Standard zurücksetzen are reset [everything]. '
              'reset my taste is unsupported, op null. Never reset learned taste, ratings or viewing history. '
              'Row commands affect the household: row_rename values [row id,new label], row_hide/row_show [row id], row_move [row id,before row id]. '
              'Row ids: episodes,foryou,week,upcoming,catchup,rewatch,because,leap,top10,people,moods,lanes,docs,studios,anime,shelves,digital,toprated,newtoyou. '
              'Top 10 maps to top10, top rated to toprated, Latest Digital Releases to digital, For You to foryou, New to You to newtoyou. '
              'shelf_add kind null, values [theme, always] or [theme, days, hours] (comma-separated mon,tue,wed,thu,fri,sat,sun or weekdays/weekend/daily; clock hour window 05-12 or 18-24). '
              'Creating, adding or making a catalog, row, shelf or list about/for/with/of a theme is shelf_add [theme,always]. '
              'German examples: neue Reihe mit, erstelle einen Katalog über. Preserve theme text and typos. Best catalog of crime movies is a one-off search, op null. '
              'German days include montags, freitags, wochentags, am Wochenende; morgens means 05-12, abends means 18-24. '
              'shelf_stop kind null, values [shelf name]; shelf_count kind null, values [count 0..10] or [fewer]. '
              'Shelf person: member name, together for us, or null for me. Title names are not commands. '
              'Extra ops add only their own keys: ' + ' '.join(v['hint'] for v in REGISTRY.values() if v['hint']) + ' '
              'When in doubt whether text is a command at all (a title, chat, a one-off search), return op null. Unsupported settings return op null. Do not invent an operation.')
def register(s):
    prompt = command_prompt(s)
    model = getattr(s, 'COMMAND_MODEL', s.PICK_MODEL)
    effort = getattr(s, 'COMMAND_EFFORT', 'low')
    fallback_model = getattr(s, 'COMMAND_FALLBACK_MODEL', 'z-ai/glm-5.3-flash')
    fallback_effort = getattr(s, 'COMMAND_FALLBACK_EFFORT', 'low')
    def request(inp, model, effort):
        return {'model': model, 'messages': [{'role': 'system', 'content': prompt},
                                           {'role': 'user', 'content': command_state(inp)}],
                'response_format': {'type': 'json_object'}, 'reasoning': {'effort': effort},
                'max_tokens': 200 if effort == 'none' else 1200}
    def parse(reply):
        return parse_json(reply['choices'][0]['message']['content'])
    def rule(inp, params):
        if inp.get('last') is not None:
            return None
        answer = parse_local(inp['query'], s)
        q = inp['query'].strip().casefold()
        return (answer, 1) if answer['op'] or ABOUT.fullmatch(q) or q.startswith('stop ') or q in ('sunday bloody sunday', 'heist', 'yes man', 'reset my taste') or re.fullmatch(r'best (?:catalog|row|shelf|list) (?:of|about|for|with) .+', q) else None
    def fallback_request(batch):
        body = request(batch[0], fallback_model, fallback_effort)
        body['max_tokens'] = 1200
        return body
    s.decision('parse_command', ver=10, lane='search', cap=None, ttl=30*s.DAY,
        inp={'query': str, 'last': (str, None), 'shown': (str, [str, 30], None)}, out=OUT, norm=s.route_norm, rule=rule, params={},
        llm={'path': '/api/v1/chat/completions', 'model': model, 'batch': 1, 'timeout': 6,
             'prompt': lambda b: request(b[0], model, effort),
             'parse': lambda r, b: [(parse(r), 1)],
             'fallback': {'path': '/api/v1/chat/completions', 'model': fallback_model, 'batch': 1, 'timeout': 6,
                          'prompt': fallback_request,
                          'parse': lambda r, b: [(parse(r), 1)]}},
        fallback=lambda i, e: (dict(NONE), 0), requires=('OR_KEY',), serving=False, min_acc=.95)
def command_state(inp):
    shown = inp.get('shown') or ''
    if isinstance(shown, list):
        shown = ', '.join(shown)
    return f"Last search: {inp['last']}\nShown titles: {shown}\nTyped now: {inp['query']}" if inp.get('last') is not None else inp['query']
def pending(s):
    if not hasattr(s, '_commands_pending'):
        s._commands_pending = {}
    return s._commands_pending
def get_pending(s, scope):
    at, plan = pending(s).get(scope, (0, None))
    if s.time.time() - at >= 600:
        pending(s).pop(scope, None)
        return None
    return plan
def people(s, named=None, viewer=None, bound=None):
    if bound == s.GUEST or viewer == s.GUEST or (s.kids_age(bound) is not None or s.kids_age(viewer) is not None):
        raise PermissionError('Ask a grown-up to change settings')
    who = bound if bound in s.PEOPLE else named or viewer
    who = who.casefold() if isinstance(who, str) else who
    who = next((p for p in s.PEOPLE if who in (p, s.LABELS[p].casefold())), who)
    if who == 'everyone':
        return [p for p in s.PEOPLE if s.kids_age(p) is None]
    if who not in s.PEOPLE:
        raise ValueError('Add a member name to this command')
    if s.kids_age(who) is not None:
        raise PermissionError('Ask a grown-up to change settings')
    return [who]
def config(s):
    return s.validate_config(json.loads(Path(os.environ.get('SKALD_CONFIG') or Path(s.DATA)/'skald.json').read_text()), data=s.DATA)
def edit(cfg, person, change):
    prefs = cfg['taste'].setdefault('people', {}).setdefault(person, {}).setdefault('prefs', {})
    op, kind, values = change['op'], change['kind'], change['values']
    if op in ('less', 'more'):
        if kind not in ('genre', 'movie', 'series') or kind == 'genre' and not values or kind != 'genre' and values:
            raise ValueError('soft weight kind or values')
        weights = prefs.setdefault('weights', {})
        if kind == 'genre':
            genres = weights.setdefault('genre', {})
            for genre in values:
                genres[genre] = -1 if op == 'less' else 1
        else:
            weights[kind] = -1 if op == 'less' else 1
        return
    if op in ('runtime', 'kids_age'):
        if len(values) != 1 or type(values[0]) is not int:
            raise ValueError(op)
        if op == 'runtime':
            if kind not in ('movie', 'series') or not 1 <= values[0] <= 600:
                raise ValueError('runtime kind or minutes (1..600)')
            prefs.setdefault('runtime', {})[kind] = values[0]
        else:
            if kind is not None or not 3 <= values[0] <= 17:
                raise ValueError('kids_age (3..17)')
            prefs['kids_age'] = values[0]
        return
    if op in ('discovery', 'years'):
        if op == 'discovery' and len(values) != 1:
            raise ValueError('discovery')
        prefs[op] = values[0] if op == 'discovery' else values
        return
    target, key = prefs, 'hide'
    if op in ('exclude_genre', 'include_genre'):
        if kind is None:
            for stype in ('movie', 'series'):
                edit(cfg, person, {**change, 'kind': stype})
            return
        target, key = prefs.setdefault('excluded_genres', {}), kind
    elif op in ('focus', 'unfocus'):
        target, key = prefs.setdefault('focus', {}), kind
        if kind in ('movie', 'series'):
            target = target.setdefault('genres', {})
    elif op not in ('hide', 'show'):
        raise ValueError('unsupported command')
    old = target.get(key, [])
    target[key] = list(dict.fromkeys(old + values)) if op in ('hide', 'exclude_genre', 'focus') else [v for v in old if v.casefold() not in {str(x).casefold() for x in values}]
def resolve(s, change):
    title = change['values'][0]
    if change['op'] == 'seen':
        hits = []
        for kind in (('movie', 'tv') if change['kind'] is None else (change['kind'],)):
            hit = s.resolve_pick(kind, title, None, s.LANG)
            if hit:
                item, _ = hit
                hits.append({'key': 'tmdb:'+str(item['id']), 'kind': kind,
                             'title': item.get('title') or item.get('name'),
                             'year': (item.get('release_date') or item.get('first_air_date') or '')[:4]})
        if len(hits) != 1:
            raise ValueError('Unknown or ambiguous title; specify movie or series')
        return hits[0]
    choices = []
    for kind in (('movie', 'tv') if change['kind'] is None else (change['kind'],)):
        for x in s.tmdb('search/'+kind, query=title).get('results', [])[:4]:
            choices.append({'key': 'tmdb:'+str(x['id']), 'kind': kind, 'title': x.get('title') or x.get('name'),
                            'year': int((x.get('release_date') or x.get('first_air_date') or '0')[:4]) or None,
                            'aliases': [], 'pop': float(x.get('popularity') or 0)})
    pick = s.decide('resolve_title', {'title': title, 'year': None, 'kind': change['kind'], 'choices': choices})['answer']['pick']
    if not pick:
        raise ValueError('Skald cannot resolve this title yet')
    item = next(x for x in choices if x['key'] == pick['key'] and x['kind'] == pick['kind'])
    return {**pick, 'title': item['title'], 'year': str(item['year'] or '')}
def preview(s, text, viewer=None, bound=None, answer=None, scope=None, prior=None, base=None, results=None):
    local = dict(NONE) if results else parse_local(text, s, viewer, bound)
    if not results and text.strip().casefold().startswith('stop ') and not local['op']:
        return None
    inp = {'query': text}
    if results:
        inp.update(last=results['query'], shown=[x['title'] for x in s.shown_titles(results)])
    change = copy.deepcopy(answer if answer is not None else local if local['op'] else s.decide('parse_command', inp)['answer'])
    change = {**dict.fromkeys(OUT), **change}
    s.check(OUT, change)
    change['values'] = change['values'] or []
    if change['op'] in ('like_results', 'shelf_from_results'):
        return results_preview(s, text, change, viewer, bound, results)
    extra = EXTRA.get(REGISTRY.get(change['op'], {}).get('group'))
    if extra:
        return extra(s, text, change, viewer, bound, scope, prior)
    if change['op'] == 'reset':
        return reset_preview(s, text, change, viewer, bound)
    if change['op'] == 'redo' or change['op'] == 'undo' and change['values']:
        return history_preview(s, text, change, viewer, bound)
    if change['op'] in ('settings', 'shelf_list', 'history'):
        if change['kind'] is not None or change['values']:
            raise ValueError('read-only settings command')
        target = shelf_scope(s, change['person'], viewer, bound)
        if change['op'] == 'history':
            details = [relative_time(s.time.time(), e.get('t'))+': '+e['name']+(' (undone)' if e.get('undone') else '')
                       for e in reversed(journal(s).get(target, []))]
            return dict(read_only=True, people=[target], name='History for '+scope_label(s, target),
                        description='\n'.join(details) or 'No changes yet')
        from . import shelves
        details = []
        if change['op'] == 'settings':
            details.append(json.dumps(s.CONFIG['taste']['people'].get(target, {}).get('prefs', {}), ensure_ascii=False))
            details.append('Household rows: '+json.dumps(row_settings(s.CONFIG), ensure_ascii=False))
        details.extend(w['name']+'; '+str(w['when']) for w in shelves.read(s)['wishes'] if w['scope'] in (target, 'together'))
        return dict(read_only=True, people=[target], name=('Settings' if change['op'] == 'settings' else 'Shelves')+' for '+('Together' if target == 'together' else s.LABELS[target]), description='\n'.join(details) or 'No shelves yet')
    if change['op'] and change['op'].startswith('shelf_'):
        return {**shelf_preview(s, change, viewer, bound), 'text': text[:200], 'source': 'search'}
    people(s, viewer=viewer, bound=bound) if bound in (*s.PEOPLE, s.GUEST) or viewer in s.PEOPLE else None
    if change['op'] is None:
        return None
    targets = [shelf_scope(s, change['person'], viewer, bound)] if change['op'] in ('undo', 'kids_age') else people(s, change['person'], viewer, bound)
    cfg = copy.deepcopy(base) if base is not None else config(s)
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets}
    item = None
    if change['op'] in ('like', 'dislike', 'seen'):
        if len(change['values']) != 1 or change['kind'] not in (None, 'movie', 'tv') or not isinstance(change['values'][0], str) or not change['values'][0].strip():
            raise ValueError('title')
        item = resolve(s, change)
    elif change['op'].startswith('row_'):
        if len(targets) != 1:
            raise ValueError('Choose one adult for household rows')
        edit_rows(cfg, change, s)
        s.validate_config(cfg, data=s.DATA)
    elif change['op'] != 'undo':
        if not change['values'] and change['op'] not in ('less', 'more'):
            raise ValueError('values')
        probe = copy.deepcopy(change)
        probe['op'] = REGISTRY[change['op']]['inverse'] if REGISTRY[change['op']]['removes'] else change['op']
        for p in targets:
            trial = copy.deepcopy(cfg)
            edit(trial, p, probe)
            s.validate_config(trial, data=s.DATA)
            edit(cfg, p, change)
    elif change['kind'] is not None or change['values']:
        raise ValueError('undo')
    op, kind, values = change['op'], change['kind'], change['values']
    display = ', '.join(str(v) for v in values)
    if op in ('exclude_genre', 'include_genre', 'focus', 'unfocus'):
        names = s.LANGUAGE_NAMES if kind == 'languages' else s.COUNTRY_NAMES if kind == 'countries' else {g.casefold(): g for g in s.TMDB_GENRES.get(kind, {}).values()}
        display = ', '.join(names.get(str(v).lower() if kind != 'countries' else str(v).upper(), str(v)) for v in values)
    if op == 'years':
        display = ' to '.join(str(v) if v is not None else 'any' for v in values)
    if item:
        display = item['title']
    if op in ('less', 'more') and not values:
        display = kind
    if op == 'runtime':
        display = kind+' up to '+display+' minutes'
    verb = REGISTRY[op]['verb'] or op.capitalize()
    name = verb+' '+display+' for '+', '.join(s.LABELS[p] for p in targets)
    if op.startswith('row_'):
        if op == 'row_rename':
            name = 'Rename '+values[0]+' to '+values[1]
        elif op == 'row_move':
            name = 'Move '+values[0]+' before '+values[1]
        else:
            name = ('Hide ' if op == 'row_hide' else 'Show ')+values[0]
        name += ' for the household'
    plan = {'people': targets, 'change': change, 'before': before, 'item': item, 'name': name,
            'text': text[:200], 'source': 'search'}
    if op.startswith('row_'):
        plan['rows_before'] = row_settings(config(s))
    if op in ('undo', 'kids_age'):
        plan['actor'] = bound if bound in s.PEOPLE else viewer if viewer in s.PEOPLE else change['person']
        people(s, viewer=plan['actor'])
    if op == 'undo':
        plan['undo_before'] = undo_stamp(journal(s), targets)
    plan['plan'] = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return plan
def results_preview(s, text, change, viewer, bound, results):
    items = s.shown_titles(results) if results else []
    if not items or change['kind'] is not None or change['values'] or change['person'] is not None:
        raise ValueError('Search for titles first; use the shown results for yourself')
    for item in items:
        s.check({**s.TITLE, 'title': str, 'year': (str, None)}, item)
    if change['op'] == 'shelf_from_results':
        plan = shelf_preview(s, dict(op='shelf_add', kind=None, values=[results['query'][:100], 'always'], person=None), viewer, bound)
        plan.update(seeds=items, text=text[:200], source='search')
    else:
        targets = people(s, viewer=viewer, bound=bound)
        cfg = config(s)
        parts = []
        for item in items:
            s.validate_feedback({**item, 'year': item.get('year') or '', 'who': targets[0], 'action': 'love'})
            part = dict(people=targets, change=dict(op='like', kind=item['kind'], values=[item['title']], person=None),
                        before={p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets},
                        item={**item, 'year': item.get('year') or ''}, name='Like '+item['title']+' for '+', '.join(s.LABELS[p] for p in targets), text=text[:200], source='search')
            part['plan'] = hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()[:16]
            parts.append(part)
        plan = dict(people=targets, result_plans=parts, name='Like shown titles for '+', '.join(s.LABELS[p] for p in targets))
    plan['description'] = '\n'.join(x['title']+(' ('+x['year']+')' if x.get('year') else '') for x in items)
    return seal(plan)
def plan_preview(s, text, change, viewer, bound, scope, prior):
    steps = change['steps'] or []
    if len(steps) < 2 or any(REGISTRY.get(x['op'], {}).get('group') not in ('prefs', 'rows') for x in steps):
        raise ValueError('A plan needs two or more setting changes')
    cfg, running, parts = config(s), config(s), []
    for x in steps:
        parts.append(preview(s, text, viewer, bound, answer={**x, 'person': x['person'] or change['person']}, base=running))
        for p in ([None] if x['op'].startswith('row_') else parts[-1]['people']):
            edit_rows(running, parts[-1]['change'], s) if p is None else edit(running, p, parts[-1]['change'])
    targets = list(dict.fromkeys(p for part in parts for p in part['people']))
    plan = dict(people=targets, change=change, steps=[[part['change'], part['people']] for part in parts],
                before={p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets},
                item=None, name='; '.join(part['name'] for part in parts), text=text[:200], source='search')
    if any('rows_before' in part for part in parts):
        plan['rows_before'] = row_settings(cfg)
    return seal(plan)

def fix(old, change):
    """Swap the person or one value in the last change; plans fix every step that matches."""
    new = copy.deepcopy(old)
    hit = False
    for step in (new['steps'] if new['op'] == 'plan' else [new]):
        if change['person']:
            step['person'], hit = change['person'], True
            if change['person'] == 'together' and not step['op'].startswith('row_'):
                step['person'] = 'everyone'
        values = [str(v) for v in change['values']]
        right = values[-1] if values else None
        if right and (step['op'] in ('exclude_genre', 'include_genre') or step['kind'] == 'genre'):
            values = [GENRES.get(v.casefold(), v) for v in values]
            right = values[-1].title()
        if len(values) == 2 and any(str(v).casefold() == values[0].casefold() for v in step['values']):
            step['values'] = [right if str(v).casefold() == values[0].casefold() else v for v in step['values']]
            hit = True
        elif len(values) == 1 and len(step['values']) == 1 and isinstance(step['values'][0], str):
            step['values'], hit = [right], True
    if not hit:
        raise ValueError('Nothing to correct; say the full change')
    if new['op'] == 'plan' and change['person']:
        new['person'] = change['person']
    return new

def correct_preview(s, text, change, viewer, bound, scope, prior):
    old = (prior or {}).get('change') if REGISTRY.get(((prior or {}).get('change') or {}).get('op'), {}).get('group') in ('prefs', 'rows', 'plan') else None
    targets, entry = getattr(s, '_commands_last', {}).get(scope) or [], None
    if old is None and targets:
        active = [e for e in journal(s).get(targets[0], []) if not e.get('undone')]
        entry = active[-1] if active else None
        if entry and REGISTRY.get((entry.get('change') or {}).get('op'), {}).get('group') in ('prefs', 'rows', 'plan'):
            old = entry['change']
    if old is None:
        raise ValueError('Nothing to correct; say the full change')
    plan = preview(s, text, viewer, bound, answer=fix(old, change))
    if prior is not None and old is prior.get('change'):
        plan.update({k: prior[k] for k in ('undo_first',) if k in prior})
        plan['name'] = ('Fix: ' if 'undo_first' in plan else '')+plan['name']
    else:
        plan.update(undo_first=dict(people=targets, stamp=undo_stamp(journal(s), targets), viewer=viewer, bound=bound),
                    name='Fix: '+plan['name'])
    return seal(plan)

def ask_preview(s, text, change, *_):
    question = change['question'] if (change['question'] or '').strip().endswith('?') else SLOTS.get(change['slot'], SLOTS['change'])
    return dict(read_only=True, people=[], name=question, description='Say the change again with that detail.')

def search_preview(s, text, change, *_):
    return dict(read_only=True, search=True, people=[], name='Search: '+', '.join(map(str, change['values'])), description='Nothing changes.')

def wish_preview(s, text, change, viewer, bound, *_):
    if len(change['values']) != 1 or not isinstance(change['values'][0], str) or not change['values'][0].strip():
        raise ValueError('values')
    who = shelf_scope(s, change['person'], viewer, bound)
    return seal(dict(people=[who], wish=dict(who=who, text=change['values'][0].strip()[:200]),
                     name='Note wish for '+scope_label(s, who)+': '+change['values'][0].strip()[:200], text=text[:200], source='search'))

def member_state(cfg, name):
    """Only this member's own values, so changes to other members never conflict."""
    hh = cfg['household']
    return dict(member=name in hh['members'], label=hh['labels'].get(name), row=name in hh.get('member_rows', []),
                experience=copy.deepcopy(hh.get('experiences', {}).get(name)),
                person=copy.deepcopy(cfg['taste'].get('people', {}).get(name)))

def set_member(cfg, name, state):
    hh = cfg['household']
    if state['member'] != (name in hh['members']):
        hh['members'] = [*hh['members'], name] if state['member'] else [p for p in hh['members'] if p != name]
        hh['labels']['pair'] = 'you both' if len(hh['members']) == 2 else 'everyone'
        if hh.get('trakt_member') == name and not state['member']:
            hh['trakt_member'] = hh['members'][0]
    for key, value in (('labels', state['label']), ('experiences', state['experience'])):
        hh.setdefault(key, {}).pop(name, None)
        if value is not None:
            hh[key][name] = copy.deepcopy(value)
    hh['member_rows'] = [p for p in hh.get('member_rows', []) if p != name] + ([name] if state['row'] else [])
    cfg['taste']['people'].pop(name, None)
    if state['person'] is not None:
        cfg['taste']['people'][name] = copy.deepcopy(state['person'])
    return cfg

def member_preview(s, text, change, viewer, bound, *_):
    from .setup import household
    actor = people(s, viewer=viewer, bound=bound)[0]
    cfg = config(s)
    name, age, label = str(change['person'] or '').strip().casefold(), change['age'], change['label']
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) or age is None and label is None and change['op'] == 'member_update':
        raise ValueError('Add a member name and an age or display name')
    known = {p: p for p in cfg['household']['members']} | {v.casefold(): p for p, v in cfg['household']['labels'].items() if p != 'pair'}
    if change['op'] == 'member_create':
        if name in known:
            raise ValueError(name.title()+' is already a member')
        cfg = household(s.DATA, [*cfg['household']['members'], name], None, None, None, existing=True, prepare=True)
    elif name not in known:
        raise ValueError('Unknown member name')
    name = known.get(name, name)
    if label is not None:
        cfg['household']['labels'][name] = label.strip()[:40]
    if age is not None:
        prefs = cfg['taste']['people'].setdefault(name, {}).setdefault('prefs', {})
        if age < 18:
            prefs['kids_age'] = age
        else:
            prefs.pop('kids_age', None)
    cfg = s.validate_config(cfg, data=s.DATA)
    detail = ', '.join(x for x in (label and 'display name '+label, age is not None and 'age '+str(age)) if x)
    title = ('Add member ' if change['op'] == 'member_create' else 'Update member ')+s.LABELS.get(name, name.title())
    return seal(dict(people=[actor], change=change, member=dict(name=name, before=member_state(config(s), name), after=member_state(cfg, name)),
                     actor=actor, name=title+(': '+detail if detail else ''), text=text[:200], source='search'))

def rule_preview(s, text, change, viewer, bound, *_):
    from . import shelves
    scope = shelf_scope(s, change['person'], viewer, bound)
    if len(change['values']) != 1 or not isinstance(change['values'][0], str):
        raise ValueError('invalid shelf command')
    if change['op'] == 'shelf_schedule':
        months = sorted(set(change['months'] or []))
        if not months or not all(1 <= m <= 12 for m in months):
            raise ValueError('invalid shelf months')
        plan = shelf_preview(s, {**change, 'op': 'shelf_add', 'values': [change['values'][0], 'always']}, viewer, bound)
        when = ', '.join(MONTHS[m-1][0].title() for m in months)
        return {**plan, 'months': months, 'name': plan['name'].replace('; always on', '; '+when+' only'), 'text': text[:200], 'source': 'search'}
    hits = shelf_hits(s, change['values'][0], scope)
    if len(hits) != 1:
        raise ValueError('unknown or ambiguous shelf name')
    filters = {k: v for k, v in (change['filters'] or {}).items() if v not in (None, [])}
    whole = [h for h in filters.get('hide') or [] if h in ('movies', 'series')]
    if whole:  # Hiding a whole type keeps only the other type.
        filters['kind'] = 'series' if whole == ['movies'] else 'movie' if whole == ['series'] else None
        filters['hide'] = [h for h in filters['hide'] if h not in whole]
        filters = {k: v for k, v in filters.items() if v not in (None, [])}
    if not filters or filters.get('years') and len(filters['years']) != 2:
        raise ValueError('invalid shelf filter')
    wish, rules = hits[0], shelves.rules(s)
    shelf_scope(s, wish['scope'], viewer, bound)
    before = rules.get(wish['id'])
    detail = '; '.join(k.replace('_', ' ')+' '+(' to '.join(str(x) for x in v) if isinstance(v, list) else str(v)) for k, v in filters.items())
    return seal(dict(people=[wish['scope']], change=change, rule=dict(id=wish['id'], before=before, after={**(before or {}), 'filters': filters}),
                     name='Limit shelf '+wish['name']+': '+detail, text=text[:200], source='search'))

# Title fixes live in title_fixes.json: defer rows hide a title for one person until a time, ignore rows stop one
# watch shaping one person's taste, move rows give one watch to another member. Feedback and history stay as they were.
FIXES = 'title_fixes.json'
CHIPS = {'too dark': r'dark|düster|finster|mra[čc]n|мрачн|bleak|grim', 'too slow': r'slow|langsam|zäh|spor|спор|boring|langweilig|dosad',
         'too violent': r'violen|gewalt|brutal|nasil|насил|gory|blutig', 'too silly': r'silly|albern|blöd|dumb|glup|глуп|stupid|kindisch',
         'not my genre': r'genre|žanr|жанр', 'seen enough of these': r'seen enough|enough of these|zu viele davon|dosta (?:mi je )?(?:takvih|toga)'}

def read_fixes(s):
    path = Path(s.DATA)/FIXES
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict) or not isinstance(doc.get('fixes'), list):
        raise ValueError(FIXES+' is damaged; restore it from a backup')
    return doc['fixes']

def save_fixes(s, rows):
    """Expired deferrals are dropped on every write; the file holds member names, so it is private."""
    from .intake import write_private
    now = s.time.time()
    rows = [r for r in rows if r.get('op') != 'defer' or r.get('until', 0) > now]
    write_private(Path(s.DATA)/FIXES, json.dumps({'fixes': rows}, ensure_ascii=False, indent=1)+'\n')

def until_time(s, until, now=None, local=None):
    """tonight ends at the next 05:00 local time; a date hides until that day starts, after today and within a year."""
    import datetime as dt
    now = s.time.time() if now is None else now
    local = s.local_now(now) if local is None else local
    if until == 'tonight':
        end = local.replace(hour=5, minute=0, second=0, microsecond=0)
        end += dt.timedelta(days=1) if local >= end else dt.timedelta()
        return wall_time(s, end, now, local)
    try:
        day = dt.date.fromisoformat(until) if isinstance(until, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', until) else None
    except ValueError:
        day = None
    if day is None:
        raise ValueError('Until when? Say tonight or a date like 2026-12-24')
    if not local.date() < day <= local.date() + dt.timedelta(days=366):
        raise ValueError('Pick a date after today and within a year')
    return wall_time(s, dt.datetime.combine(day, dt.time()), now, local)

def wall_time(s, target, now, local):
    """The moment local clocks show target, counting a daylight saving change between now and then."""
    import datetime as dt
    def offset(t):
        return (s.local_now(t) - dt.datetime.fromtimestamp(t, dt.timezone.utc).replace(tzinfo=None)).total_seconds()
    guess = now + (target - local).total_seconds()
    return guess - (offset(guess) - offset(now))

def find_title(s, change):
    """A title the household watched wins; otherwise one TMDB match."""
    title = change['values'][0].strip()
    hits = {(e['key'], e['kind']): e for e in (getattr(s, '_profiles', {}).get('household') or {}).get('entries', [])
            if str(e.get('title', '')).casefold() == title.casefold() and change['kind'] in (None, e.get('kind'))}
    if len(hits) == 1:
        e = next(iter(hits.values()))
        return {'key': e['key'], 'kind': e['kind'], 'title': e['title'], 'year': str(e.get('year') or '')}
    return resolve(s, {**change, 'op': 'seen'})

def watched_by(s, key):
    return [e for e in (getattr(s, '_profiles', {}).get('household') or {}).get('entries', []) if e['key'] == key]

def explain_text(s, item, who):
    info = s.explain(item['key'], item['kind'])
    label = s.LABELS.get(who, 'you') if who else 'the household'
    lines = []
    if info.get('reasons'):
        lines.append('Because: '+'; '.join(str(r) for r in info['reasons']))
    mine = (info.get('per_person') or {}).get(who) or {}
    pct = mine.get('percentile', info.get('percentile'))
    if pct is not None:
        lines.append('Match for '+label+': '+str(pct)+' of 100')
    if (mine.get('memory') or {}).get('vetoed'):
        lines.append('A remembered dislike of '+label+' keeps titles like this out of the rows.')
    if who in s.PEOPLE and item['key'] in s.deferred_keys(who):
        lines.append('Hidden for '+label+' for now; it comes back by itself.')
    if 'model' in info and info['model'] is None:
        lines.append('No taste model yet; Skald needs more watches before it can explain picks.')
    return '\n'.join(lines) or 'No reasons recorded for this title yet.'

def title_preview(s, text, change, viewer, bound, *_):
    op = change['op']
    if len(change['values']) != 1 or not isinstance(change['values'][0], str) or not change['values'][0].strip() or change['kind'] not in (None, 'movie', 'tv'):
        raise ValueError('Which title do you mean?')
    if op == 'explain':
        who = bound if bound in s.PEOPLE else viewer if viewer in s.PEOPLE else None
        item = find_title(s, change)
        return dict(read_only=True, people=[], name='Why '+item['title']+' is in the picks', description=explain_text(s, item, who))
    targets = people(s, None if op == 'reattribute' else change['person'], viewer, bound)
    to = None
    if op == 'reattribute':
        to = next((p for p in s.PEOPLE if str(change['person'] or '').casefold() in (p, s.LABELS[p].casefold())), None)
        if to is None or to in targets or len(targets) != 1:
            raise ValueError('Name the other member who really watched it')
    until = until_time(s, change['until']) if op == 'defer' else None
    item = find_title(s, change)
    title, labels = item['title'], ', '.join(s.LABELS[p] for p in targets)
    base = dict(people=targets, change=change, item=item, text=text[:200], source='search')
    if op == 'reject':
        reason = (change['reason'] or '').strip()[:200] or None
        chips = [c for c, pattern in CHIPS.items() if reason and re.search(pattern, reason, re.I)]
        active, plan = s.active_feedback(s.read_feedback()), {}
        for p in targets:
            old = active.get((item['key'], p)) or {}
            have = old.get('reasons', []) if old.get('action') == 'not_for_us' else []
            if old.get('action') != 'not_for_us' or set(chips) - set(have):
                plan[p] = dict(was=old.get('id'), reasons=[*have, *(c for c in chips if c not in have)])
        if not plan and reason:
            return seal(dict(base, reject={}, note=True, reason=reason,
                             name='Already not for '+labels+': '+title+'; note the reason ('+reason+')'))
        if not plan:
            return dict(read_only=True, people=[], name='Already not for '+labels+': '+title, description='Nothing changes.')
        return seal(dict(base, people=list(plan), reject=plan, reason=reason,
                         name='Not for '+', '.join(s.LABELS[p] for p in plan)+': '+title+(' ('+reason+')' if reason else '')))
    rows = read_fixes(s)
    kind = {'defer': 'defer', 'ignore_learning': 'ignore', 'reattribute': 'move'}[op]
    fixes = []
    for p in targets:
        if kind != 'defer':
            if any(r.get('op') in ('ignore', 'move') and r['key'] == item['key'] and r['who'] == p for r in rows):
                continue
            mine = [e for e in watched_by(s, item['key']) if p in s.watch_owners(e) or kind == 'ignore' and 'together' in e.get('by', ())]
            if not mine:
                raise ValueError('No watch of '+title+' by '+s.LABELS[p]+' to '+('move' if kind == 'move' else 'ignore'))
        row = dict(op=kind, key=item['key'], kind=item['kind'], title=title, who=p)
        if kind == 'move':
            row['to'] = to
        if kind == 'defer':
            row.update(until=round(until), until_text=change['until'])
        fixes.append(row)
    if not fixes:
        return dict(read_only=True, people=[], name='Already done for '+labels+': '+title, description='Nothing changes.')
    when = 'tonight' if change['until'] == 'tonight' else change['until']
    name = {'defer': 'Hide '+title+' for '+labels+' until '+str(when)+'; it comes back by itself',
            'ignore': 'Stop learning from '+title+' for '+labels+'; the watch stays in history',
            'move': 'Move the watch of '+title+' from '+labels+' to '+s.LABELS.get(to, '')}[kind]
    return seal(dict(base, people=[r['who'] for r in fixes], fix=fixes, name=name))

def apply_title(s, plan):
    """Write fix rows or not_for_us feedback, then one journal entry per person for plain undo."""
    from . import history
    for p in plan['people']:
        people(s, viewer=p)
    current, entries, events, added = config(s), journal(s), {}, []
    if plan.get('fix'):
        with history.locked(s.DATA):
            rows = read_fixes(s)
            if any(r.get('op') != 'defer' and (r.get('op'), r['key'], r['who']) in {(x['op'], x['key'], x['who']) for x in plan['fix']} for r in rows):
                raise ValueError('Already changed; preview again')
            added = [{**r, 'id': uuid.uuid4().hex, 't': int(s.time.time())} for r in plan['fix']]
            save_fixes(s, rows + added)
    if plan.get('reject'):
        active = s.active_feedback(s.read_feedback())
        if any((active.get((plan['item']['key'], p)) or {}).get('id') != r['was'] for p, r in plan['reject'].items()):
            raise ValueError('Ratings changed; preview again')
        for p, r in plan['reject'].items():
            events[p] = s.write_feedback({**plan['item'], 'who': p, 'action': 'not_for_us', 'reasons': r['reasons']})
    for p in plan['people']:
        prefs = current['taste']['people'].get(p, {}).get('prefs')
        entry = history_entry(s, plan, prefs, prefs, fix=[r for r in added if r['who'] == p] or None)
        entry['event'] = events.get(p)
        if 'reject' in plan:
            entry['reason'] = plan.get('reason')
            entry['note'] = plan.get('note')
        entries[p] = (entries.get(p, []) + [entry])[-200:]
    save_journal(s, entries)
    if any(r['op'] == 'defer' for r in added):
        s.aio_rows_changed()
    return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}

EXTRA = {'plan': plan_preview, 'correct': correct_preview, 'ask': ask_preview, 'search': search_preview,
         'wish': wish_preview, 'member': member_preview, 'rule': rule_preview, 'title': title_preview}

def journal(s):
    path = Path(s.DATA)/'commands.json'
    entries = json.loads(path.read_text()) if path.exists() else {}
    cutoff = s.time.time() - 180*86400
    for rows in entries.values():
        for entry in rows:
            for key, value in dict(t=None, text='', source=None, change=None, after=None).items():
                entry.setdefault(key, value)
    return {p: [e for e in rows if e.get('t') is None or e['t'] >= cutoff][-200:]
            for p, rows in entries.items()}

def scope_label(s, scope):
    return 'Together' if scope == 'together' else s.LABELS[scope]

def relative_time(now, then):
    if then is None:
        return 'Older change (date unknown)'
    seconds = max(0, int(now - then))
    for unit, size in (('year', 365*86400), ('month', 30*86400), ('week', 7*86400),
                       ('day', 86400), ('hour', 3600), ('minute', 60)):
        if seconds >= size:
            n = seconds // size
            return str(n)+' '+unit+('s' if n != 1 else '')+' ago'
    return 'Just now'

def seal(plan):
    plan['plan'] = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return plan

def history_match(entries, query, redo=False):
    def words(text):
        return re.findall(r'\w+', str(text).casefold())
    tokens = set(words(query)) - {'the', 'shelf', 'row', 'regal', 'reihe'}
    hits = []
    for i, entry in enumerate(entries):
        if entry.get('undone') and not redo:
            continue
        change = entry.get('change') or {}
        fields = [entry.get('text', ''), entry['name'], str(change.get('op', '')).replace('_', ' '),
                  *change.get('values', [])]
        if change.get('text'):
            fields.append(change['text'])
        found = set(words(' '.join(map(str, fields))))
        if tokens and tokens <= found:
            exact = any(words(query) == words(field) for field in fields)
            hits.append((exact, i, entry))
    if not hits:
        raise ValueError('No matching settings change in history')
    return max(hits, key=lambda hit: hit[:2])[2]

def inverse_prefs(current, before, after, path=()):
    """Invert only this entry's delta; unrelated current values survive."""
    result = copy.deepcopy(current or {})
    before, after = before or {}, after or {}
    for key in before.keys() | after.keys():
        old, new, value = before.get(key), after.get(key), result.get(key)
        if old == new:
            continue
        here = (*path, key)
        if isinstance(old, dict) or isinstance(new, dict):
            restored = inverse_prefs(value, old, new, here)
        elif here[0] in ('hide', 'excluded_genres', 'focus'):
            removed = {str(v).casefold() for v in (new or []) if v not in (old or [])}
            restored = [v for v in (value or []) if str(v).casefold() not in removed]
            restored += [v for v in (old or []) if v not in (new or []) and v not in restored]
        else:
            if value != new:
                raise ValueError('.'.join(here)+' changed since this command; cannot undo it')
            restored = copy.deepcopy(old)
        result.pop(key, None)
        if restored is not None and (restored or key in before):
            result[key] = restored
    return result

def inverse_settings(cfg, person, before, after, change):
    inverse = REGISTRY.get(change['op'], {}).get('inverse')
    if inverse:
        edit(cfg, person, {**change, 'op': inverse})
    else:
        current = cfg['taste']['people'].get(person, {}).get('prefs')
        set_prefs(cfg, person, inverse_prefs(current, before, after) or None)

def history_preview(s, text, change, viewer, bound):
    if change['kind'] is not None or len(change['values']) != 1 or not isinstance(change['values'][0], str):
        raise ValueError('Name one settings change to undo or redo')
    scope = shelf_scope(s, change['person'], viewer, bound)
    entries = journal(s)
    entry = history_match(entries.get(scope, []), change['values'][0], change['op'] == 'redo')
    original = entry.get('change')
    if not original:
        raise ValueError('This older command supports plain undo only')
    if bound in s.PEOPLE and any(p != bound for p in entry.get('prefs_before', {})):
        raise PermissionError('This plan applies to another member')
    if change['op'] == 'redo':
        if entry.get('shelf') and original['op'] != 'reset':
            shelf_scope(s, scope, viewer, bound)
            action = {k: original[k] for k in ('text', 'name', 'when', 'slot', 'count')}
            if original['op'] == 'add':
                action['slot'] = None
            elif original['op'] == 'stop':
                removed = [w for w in entry['shelf_before']['wishes'] if w not in entry['shelf']['wishes']]
                hits = [w for w in shelf_state(s, scope)['wishes'] if any(w['id'] == old['id'] for old in removed)]
                if len(hits) != 1:
                    raise ValueError('Shelf changed; cannot redo this stop')
                action['slot'] = hits[0]['slot']
            shelf = s.shelf_change(scope, original['op'], **action)
            return dict(people=[scope], shelf=shelf, plan=shelf['plan'], name='Redo: '+entry['name'], text=text[:200], source='search',
                        **({'months': entry['months']} if entry.get('months') else {}))
        return preview(s, text, viewer, bound, answer={**original, 'person': scope})
    if entry.get('event') or entry.get('rows') or entry.get('member') or entry.get('rule') or entry.get('fix') or entry.get('note'):
        raise ValueError('This command supports plain undo only')
    actor = bound if bound in s.PEOPLE else viewer
    if not entry.get('shelf') or original['op'] == 'reset' and original['values'] != ['shelves']:
        people(s, viewer=actor)
    cfg = config(s)
    affected = entry.get('prefs_before', {scope: entry['before']} if scope in s.PEOPLE else {})
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in affected}
    for p in before:
        inverse_settings(cfg, p, affected[p], entry.get('prefs_after', {scope: entry['after']})[p], original)
    if entry.get('shelf'):
        inverse_shelves(shelf_state(s, scope), entry['shelf_before'], entry['shelf'])
    index = max(i for i, e in enumerate(entries[scope]) if e == entry)
    return seal(dict(people=[scope], change=change, before=before, target=entry, target_index=index,
                     actor=actor, undo_before=undo_stamp(entries, [scope]),
                     name='Undo: '+entry['name'], text=text[:200], source='search'))

def targeted_undo(s, plan, current, entries):
    scope = plan['people'][0]
    if undo_stamp(entries, [scope]) != plan['undo_before']:
        raise ValueError('Commands changed; preview undo again')
    entry = entries[scope][plan['target_index']]
    shelf_scope(s, scope, plan['actor'], None)
    if not entry.get('shelf') or entry['change']['op'] == 'reset' and entry['change']['values'] != ['shelves']:
        people(s, viewer=plan['actor'])
    cfg = copy.deepcopy(current)
    old_prefs = entry.get('prefs_before', {scope: entry['before']})
    new_prefs = entry.get('prefs_after', {scope: entry['after']})
    for p in plan['before']:
        inverse_settings(cfg, p, old_prefs[p], new_prefs[p], entry['change'])
    state = inverse_shelves(shelf_state(s, scope), entry['shelf_before'], entry['shelf']) if entry.get('shelf') else None
    if state:
        set_count(cfg, scope, state['count'])
    publish_state(s, cfg, scope, state)
    if entry.get('shelf_before') and entry['change']['op'] != 'reset':
        from .shelves import consume_undo
        consume_undo(s, scope, entry['shelf_before'], entry['change']['op'])
    entry['undone'] = True
    save_journal(s, entries)
    return {'name': 'Undone: '+entry['name']}

def shelf_state(s, scope):
    from . import shelves
    return dict(wishes=copy.deepcopy([w for w in shelves.read(s)['wishes'] if w['scope'] == scope]),
                count=shelves.slot_count(s, scope))

def inverse_shelves(current, before, after):
    result = copy.deepcopy(current)
    if before['count'] != after['count']:
        if current['count'] != after['count']:
            raise ValueError('Shelf count changed since this command; cannot undo it')
        result['count'] = before['count']
    old = {w['id']: w for w in before['wishes']}
    new = {w['id']: w for w in after['wishes']}
    result['wishes'] = [w for w in result['wishes'] if w['id'] not in new or w['id'] in old]
    for key, wish in old.items():
        if key not in new and not any(w['id'] == key for w in result['wishes']):
            if any(w['slot'] == wish['slot'] for w in result['wishes']) or wish['slot'] > result['count']:
                raise ValueError('Shelf slot changed since this command; cannot undo it')
            result['wishes'].append(wish)
    return result

def set_prefs(cfg, person, prefs):
    traits = cfg['taste']['people'].setdefault(person, {})
    traits.pop('prefs', None)
    if prefs is not None:
        traits['prefs'] = copy.deepcopy(prefs)

def set_count(cfg, scope, count):
    if scope == 'together':
        if cfg['household'].get('shelves', 2) != count:
            cfg['household']['shelves'] = count
    elif cfg['taste']['people'].get(scope, {}).get('prefs', {}).get('shelves', 4) != count:
        cfg['taste']['people'].setdefault(scope, {}).setdefault('prefs', {})['shelves'] = count

def publish_state(s, cfg, scope, state=None):
    from . import shelves
    from .setup import publish
    changed = shelves.counts(config(s)) != shelves.counts(cfg)
    cfg = s.validate_config(cfg, data=s.DATA)
    if state is not None:
        doc = shelves.read(s)
        changed |= shelf_state(s, scope) != state
        doc['wishes'] = [w for w in doc['wishes'] if w['scope'] != scope] + state['wishes']
        doc['plans'].clear()
        shelves.save(s, doc)
    publish(Path(s.DATA), cfg, replace=True)
    s.refresh_preferences()
    if changed:
        s.aio_rows_changed()

def reset_preview(s, text, change, viewer, bound):
    if change['kind'] is not None or change['values'] not in (['filters'], ['shelves'], ['everything']):
        raise ValueError('Reset filters, shelves or everything to default')
    scope = shelf_scope(s, change['person'], viewer, bound)
    actor = bound if bound in s.PEOPLE else viewer
    choice = change['values'][0]
    if bound in s.PEOPLE and scope == 'together' and choice != 'shelves':
        raise PermissionError('Use a shared link to reset Together filters')
    if choice != 'shelves':
        people(s, viewer=actor)
    targets = [p for p in s.PEOPLE if s.kids_age(p) is None] if scope == 'together' else [scope]
    cfg = config(s)
    before = {p: copy.deepcopy(cfg['taste']['people'].get(p, {}).get('prefs')) for p in targets}
    after, details = {}, []
    for p, prefs in before.items():
        kept = copy.deepcopy(prefs or {})
        removed = {k: v for k, v in kept.items() if (k == 'shelves') == (choice == 'shelves') or choice == 'everything'}
        if scope == 'together':
            removed.pop('shelves', None)
        for key in removed:
            kept.pop(key)
        after[p] = kept or None
        if removed:
            details.append(scope_label(s, p)+': '+json.dumps(removed, ensure_ascii=False))
    # Together shelves have their own count; personal shelf settings stay personal.
    if scope == 'together' and choice == 'shelves':
        before, after = {}, {}
    plan = dict(people=[scope], change=change, before=before, prefs_after=after, actor=actor,
                name='Reset '+choice+' to default for '+scope_label(s, scope), text=text[:200], source='search', reset=True)
    if choice != 'filters':
        state = shelf_state(s, scope)
        plan.update(shelf_before=state, shelf_after=dict(wishes=[], count=2 if scope == 'together' else 4))
        details.extend('Clear shelf: '+w['name'] for w in state['wishes'])
        details.append('Shelf count: '+str(state['count'])+' to '+str(plan['shelf_after']['count']))
        if scope == 'together':
            plan['household_shelves_before'] = cfg['household'].get('shelves')
    plan['description'] = 'Clear settings:\n'+'\n'.join(details) if details else 'Already at default'
    return seal(plan)

def apply_reset(s, plan, current, entries):
    scope = plan['people'][0]
    shelf_scope(s, scope, plan['actor'], None)
    if plan['change']['values'] != ['shelves']:
        people(s, viewer=plan['actor'])
    cfg = copy.deepcopy(current)
    for p, before in plan['before'].items():
        if current['taste']['people'].get(p, {}).get('prefs') != before:
            raise ValueError('Settings changed; preview reset again')
        set_prefs(cfg, p, plan['prefs_after'][p])
    if 'shelf_before' in plan and shelf_state(s, scope) != plan['shelf_before']:
        raise ValueError('Shelves changed; preview reset again')
    if 'household_shelves_before' in plan:
        cfg['household'].pop('shelves', None)
    entry = history_entry(s, plan, plan['before'].get(scope), plan['prefs_after'].get(scope))
    entry.update(prefs_before=plan['before'], prefs_after=plan['prefs_after'])
    if 'shelf_before' in plan:
        entry.update(shelf_before=plan['shelf_before'], shelf=plan['shelf_after'])
    if 'household_shelves_before' in plan:
        entry['household_shelves_before'] = plan['household_shelves_before']
    publish_state(s, cfg, scope, plan.get('shelf_after'))
    entries[scope] = (entries.get(scope, []) + [entry])[-200:]
    save_journal(s, entries)
    return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}

def history_entry(s, plan, before, after, **fields):
    return dict(before=copy.deepcopy(before), after=copy.deepcopy(after), shelf=None, event=None,
                name=plan['name'], t=int(s.time.time()), text=plan.get('text', '')[:200],
                source=plan.get('source', 'search'), change=copy.deepcopy(plan.get('change') or plan.get('shelf')),
                **fields)

def undo_stamp(entries, targets):
    return hashlib.sha256(json.dumps({p: entries.get(p, []) for p in targets}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
def save_journal(s, entries):
    fd, temp = tempfile.mkstemp(dir=s.DATA, prefix='.commands-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(entries, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, Path(s.DATA)/'commands.json')
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
_LOCK = threading.RLock()
def apply_rule(s, plan):
    """Member and shelf-rule changes read config and journal only under the shelf lock."""
    from . import shelves
    from .setup import publish
    p, m, r = plan['people'][0], plan.get('member'), plan.get('rule')
    with shelves.locked(s):
        s.refresh_preferences()
        current, entries = config(s), journal(s)
        cfg = copy.deepcopy(current)
        rules = shelves.rules(s)
        if m:
            people(s, viewer=plan['actor'])
            if member_state(current, m['name']) != m['before']:
                raise ValueError('Settings changed; preview again')
            cfg = s.validate_config(set_member(cfg, m['name'], m['after']), data=s.DATA)
            publish(Path(s.DATA), cfg, replace=True)
            s.refresh_preferences()
        elif rules.get(r['id']) != r['before'] or not any(w['id'] == r['id'] for w in shelves.read(s)['wishes']):
            raise ValueError('Shelves changed; preview again')
        else:
            shelves.save_rules(s, {**rules, r['id']: r['after']})
        prefs = current['taste']['people'].get(p, {}).get('prefs')
        entries[p] = (entries.get(p, []) + [history_entry(s, plan, prefs, prefs, member=m, rule=r)])[-200:]
        save_journal(s, entries)
    s.aio_rows_changed()
    return {'name': 'Done: '+plan['name']+('. Restart Skald to apply member changes' if m else '')+". Search 'undo' to revert."}

def apply(s, plan, *, cfg=None):
    from .setup import publish
    if plan.get('read_only'):
        return plan
    with _LOCK:
        from .migrate import ensure
        ensure(s.DATA)
        s.refresh_preferences()
        if plan.get('result_plans'):
            # Keep the existing one-event journal format, with one undo per title.
            for part in plan['result_plans']:
                apply(s, part)
            return {'name': 'Done: '+plan['name']+". Search 'undo' once per title to revert."}
        if plan.get('wish'):
            from . import history
            with history.locked(s.DATA), os.fdopen(os.open(Path(s.DATA)/'wishes.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a') as f:
                f.write(json.dumps({'t': int(s.time.time()), **plan['wish']}, ensure_ascii=False)+'\n')
            return {'name': 'Noted: '+plan['wish']['text']+'. No settings changed.'}
        if plan.get('fix') or plan.get('reject') or plan.get('note'):
            return apply_title(s, plan)
        if plan.get('undo_first'):
            first = plan['undo_first']
            if undo_stamp(journal(s), first['people']) != first['stamp']:
                raise ValueError('Commands changed; preview again')
            undo(s, first['people'], actor=first['viewer'])
            apply(s, preview(s, plan['text'], first['viewer'], first['bound'], answer=plan['change']))
            return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}
        if plan.get('target') or plan.get('reset'):
            from . import shelves
            with shelves.locked(s):
                s.refresh_preferences()
                current, entries = config(s), journal(s)
                return targeted_undo(s, plan, current, entries) if plan.get('target') else apply_reset(s, plan, current, entries)
        if plan.get('member') or plan.get('rule'):
            return apply_rule(s, plan)
        current, entries = config(s), journal(s)
        if plan.get('change', {}).get('op') == 'undo':
            people(s, viewer=plan['actor'])
            if undo_stamp(entries, plan['people']) != plan['undo_before']:
                raise ValueError('Commands changed; preview undo again')
            return undo(s, plan['people'], actor=plan['actor'])
        if 'shelf' in plan:
            from . import shelves
            shelf = plan['shelf']
            before_shelf = shelf_state(s, shelf['scope'])
            s.shelf_change(shelf['scope'], shelf['op'], plan=shelf['plan'])
            p = shelf['scope']
            after = {'wishes': [w for w in shelves.read(s)['wishes'] if w['scope'] == p], 'count': shelves.slot_count(s, p)}
            if plan.get('seeds'):
                # shortcut: pins use the feed cache; use durable shelf seeds if they must survive cache resets.
                wish = next(w for w in after['wishes'] if w['slot'] == shelf['slot'])
                s._feeds[s.rid(f'shelfraw_{p}_{shelf["slot"]}'), s.LANG] = {
                    'wish': wish['id'], 'window': 'always', 'status': 'pinned',
                    'candidates': [{'id': x['key'], 'type': 'movie' if x['kind'] == 'movie' else 'series',
                                    'name': x['title'], 'releaseInfo': x.get('year') or ''} for x in plan['seeds']]}
                s.save_feeds()
            if plan.get('months'):
                with shelves.locked(s):
                    added = {w['id'] for w in after['wishes']} - {w['id'] for w in before_shelf['wishes']}
                    shelves.save_rules(s, {**shelves.rules(s), **{i: {'months': plan['months']} for i in added}})
            entry = history_entry(s, plan, current['taste']['people'].get(p, {}).get('prefs'),
                                  config(s)['taste']['people'].get(p, {}).get('prefs'))
            entry['shelf'] = after
            entry['shelf_before'] = before_shelf
            if plan.get('months'):
                entry['months'] = plan['months']
            entries[p] = (entries.get(p, []) + [entry])[-200:]
            save_journal(s, entries)
            return {'name': 'Done: '+plan['name']+". Search 'undo' to revert."}
        if cfg is None:
            if 'rows_before' in plan and row_settings(current) != plan['rows_before']:
                raise ValueError('Rows changed; preview again')
            for p in plan['people']:
                people(s, viewer=plan['actor'] if plan.get('change', {}).get('op') == 'kids_age' else p)
                if current['taste']['people'].get(p, {}).get('prefs') != plan['before'][p]:
                    raise ValueError('Settings changed; preview again')
            cfg = copy.deepcopy(current)
        if 'change' in plan and not plan.get('item'):
            for change, targets in plan.get('steps') or [(plan['change'], plan['people'])]:
                if change['op'].startswith('row_'):
                    edit_rows(cfg, change, s)
                else:
                    for p in targets:
                        edit(cfg, p, change)
        cfg = s.validate_config(cfg, data=s.DATA)
        for p in plan['people']:
            event = None
            if plan.get('item'):
                event = s.write_feedback({**plan['item'], 'who': p, 'action': REGISTRY[plan['change']['op']]['feedback']})
            entry = history_entry(s, plan, current['taste']['people'].get(p, {}).get('prefs'),
                                  cfg['taste']['people'].get(p, {}).get('prefs'))
            entry['event'] = event
            if plan.get('change', {}).get('op') == 'kids_age':
                entry['kids_age'] = True
            if 'rows_before' in plan:
                entry['rows'] = {'before': row_settings(current), 'after': row_settings(cfg)}
            entries[p] = (entries.get(p, []) + [entry])[-200:]
        publish(Path(s.DATA), cfg, replace=True)
        s.refresh_preferences()
        save_journal(s, entries)
        from .shelves import counts
        if counts(current) != counts(cfg) or row_settings(current) != row_settings(cfg):
            s.aio_rows_changed()
    return {'name': 'Done: '+plan['name']+'. Search \'undo\' to revert.'}
def undo(s, targets, actor=None):
    from . import shelves
    from .setup import publish
    with _LOCK, shelves.locked(s):
        from .migrate import ensure
        ensure(s.DATA)
        s.refresh_preferences()
        cfg, entries = config(s), journal(s)
        if not targets:
            raise ValueError('Add a member name to undo')
        for p in targets:
            if p != 'together' and s.kids_age(p) is None:
                people(s, viewer=p)
            active = [e for e in entries.get(p, []) if not e.get('undone')]
            if not active:
                raise ValueError('No command to undo')
            rows = active[-1].get('rows')
            if rows and row_settings(cfg) != rows['after']:
                raise ValueError('Rows changed; cannot undo an older row command')
            settings = not active[-1].get('shelf') or (active[-1].get('change') or {}).get('op') == 'reset' and active[-1]['change']['values'] != ['shelves']
            if s.kids_age(p) is not None and settings and not (actor in s.PEOPLE and s.kids_age(actor) is None and (active[-1].get('kids_age') or active[-1].get('prefs_before') is not None)):
                raise PermissionError('Ask a grown-up to change settings')
        skipped = []
        for p in targets:
            old = next(e for e in reversed(entries[p]) if not e.get('undone'))
            if old.get('prefs_before') is not None or old.get('shelf_before'):
                if old.get('shelf') and shelf_state(s, p) != old['shelf']:
                    raise ValueError('Shelves changed; cannot undo this command')
                for target, prefs in old.get('prefs_before', {p: old['before']} if p in s.PEOPLE else {}).items():
                    if 'prefs_before' not in old or old['change'].get('values') == ['shelves']:
                        after = old.get('prefs_after', {p: old['after']})[target]
                        prefs = inverse_prefs(cfg['taste']['people'].get(target, {}).get('prefs'), prefs, after) or None
                    set_prefs(cfg, target, prefs)
                if 'household_shelves_before' in old:
                    cfg['household'].pop('shelves', None)
                    if old['household_shelves_before'] is not None:
                        cfg['household']['shelves'] = old['household_shelves_before']
                elif old.get('shelf_before'):
                    set_count(cfg, p, old['shelf_before']['count'])
                publish_state(s, cfg, p, old.get('shelf_before'))
                if old.get('shelf_before') and old['change']['op'] != 'reset':
                    shelves.consume_undo(s, p, old['shelf_before'], old['change']['op'])
                cfg = config(s)
            elif old.get('member'):
                m = old['member']
                if member_state(cfg, m['name']) != m['after']:
                    raise ValueError('Members changed; cannot undo this command')
                if not m['before']['member']:
                    from .setup import member_data
                    rest = copy.deepcopy(cfg)
                    rest['taste']['people'].pop(m['name'], None)
                    if member_data(Path(s.DATA), m['name'], rest, skip=('commands.json',)):
                        raise ValueError(m['name'].title()+' has data; remove members with skald setup and the household archive tools')
                cfg = s.validate_config(set_member(cfg, m['name'], m['before']), data=s.DATA)
            elif old.get('rule'):
                rules, r = shelves.rules(s), old['rule']
                if rules.get(r['id']) != r['after']:
                    raise ValueError('Shelves changed; cannot undo this command')
                shelves.save_rules(s, {k: v for k, v in rules.items() if k != r['id']} | ({r['id']: r['before']} if r['before'] else {}))
            elif old.get('shelf'):
                shelves._change(s, p, 'undo', apply=True, expected=old.get('shelf'))
                cfg = config(s)
            elif old.get('fix'):
                from . import history
                ids = {r['id'] for r in old['fix']}
                with history.locked(s.DATA):
                    rows = read_fixes(s)
                    if any(r.get('id') in ids for r in rows):
                        save_fixes(s, [r for r in rows if r.get('id') not in ids])
                    else:
                        skipped.append(old['name'])
            elif old.get('note'):
                pass  # Only a reason was noted; the journal entry is the whole change.
            elif old.get('rows'):
                for key, value in old['rows']['before'].items():
                    cfg['household'].pop(key, None)
                    if value is not None:
                        cfg['household'][key] = value
                if old['before'] != old['after']:
                    set_prefs(cfg, p, old['before'])
            elif old['event']:
                if any(e['id'] == old['event']['id'] for e in s.active_feedback(s.read_feedback()).values()):
                    s.write_feedback({k: v for k, v in old['event'].items() if k not in ('id', 't', 'undo')} | {'undo': old['event']['id']})
                else:
                    skipped.append(old['name'])
            else:
                traits = cfg['taste']['people'][p]
                traits.pop('prefs', None)
                if old['before'] is not None:
                    traits['prefs'] = old['before']
            old['undone'] = True
        from .shelves import counts
        count_changed = counts(config(s)) != counts(cfg) or row_settings(config(s)) != row_settings(cfg)
        publish(Path(s.DATA), cfg, replace=True)
        s.refresh_preferences()
        save_journal(s, entries)
        if count_changed:
            s.aio_rows_changed()
    return {'name': 'Undone for '+', '.join('Together' if p == 'together' else s.LABELS[p] for p in targets) + ('. Newer ratings kept; could not be undone: '+ '; '.join(skipped) if skipped else '')}
def card(s, name, stype, description='', *, title=None):
    cid = s.rid('cmd:'+hashlib.sha256(name.encode()).hexdigest()[:12])
    short = textwrap.shorten(title or name, width=24, placeholder='...')
    if short != name:
        full = re.sub(r'^yes \d{2}: ', '', name)
        description = full+('\n'+description if description else '')
    return {'metas': [{'id': cid, 'type': stype,
                       'name': short, 'description': description, 'poster': '/command.svg'}]}

def error_card(s, error, stype, query=''):
    message, detail = str(error), ''
    title = 'Check this command'
    if isinstance(error, PermissionError):
        title = 'Ask a grown-up'
    elif message.startswith('Add a member name'):
        title = 'Who is asking?'
        label = s.LABELS[s.PEOPLE[0]]
        example = 'what do you know about '+label if parse_about(query, s) is not False else query+' for '+label
        if re.search(r'\bsettings\b', query, re.I):
            example = 'settings for '+label
        detail = 'Add a member name, for example "'+example+'", or choose Who\'s watching.'
    elif 'shelf' in message.casefold():
        title = 'Check your shelves'
    elif 'preview' in message.casefold() or 'changed' in message.casefold():
        title = 'Preview again'
    elif 'title' in message.casefold():
        title = 'Which title?'
    return card(s, message, stype, detail, title=title)

def about_detail(s, person):
    from . import shelves
    traits = s.CONFIG['taste']['people'].get(person, {})
    prefs = traits.get('prefs', {})
    detail = []
    detail.append('Shows '+str(shelves.slot_count(s, person))+' shelves for '+scope_label(s, person)+'.')
    for key, values in row_settings(s.CONFIG).items():
        if values:
            text = ', '.join(row+' is '+label for row, label in values.items()) if key == 'row_labels' else ', '.join(values)
            detail.append('Household '+key.replace('_', ' ')+': '+text+'.')
    if traits.get('likes'):
        detail.append('Likes: '+traits['likes']+'.')
    if traits.get('loves'):
        detail.append('Loves: '+', '.join(traits['loves'])+'.')
    if prefs.get('hide'):
        detail.append('Avoids: '+', '.join(prefs['hide'])+'.')
    for kind, values in prefs.get('excluded_genres', {}).items():
        if values:
            detail.append('Avoids '+kind+' genres: '+', '.join(values)+'.')
    focus = prefs.get('focus', {})
    for kind, values in focus.items():
        if kind == 'genres':
            for typ, genres in values.items():
                if genres:
                    detail.append('Likes '+typ+' genres: '+', '.join(genres)+'.')
        elif values:
            names = s.LANGUAGE_NAMES if kind == 'languages' else s.COUNTRY_NAMES if kind == 'countries' else {}
            detail.append('Likes '+kind+': '+', '.join(names.get(v, v) for v in values)+'.')
    weights = prefs.get('weights', {})
    for name, weight in {**{k: v for k, v in weights.items() if k != 'genre'}, **weights.get('genre', {})}.items():
        if weight:
            detail.append(('Prefers more ' if weight > 0 else 'Prefers less ')+name+'.')
    for kind, minutes in prefs.get('runtime', {}).items():
        detail.append('Prefers '+kind+' up to '+str(minutes)+' minutes.')
    if prefs.get('years') and any(y is not None for y in prefs['years']):
        detail.append('Prefers years '+ ' to '.join(str(y) if y is not None else 'any' for y in prefs['years'])+'.')
    if prefs.get('discovery'):
        detail.append('Discovery: '+prefs['discovery']+'.')
    if prefs.get('kids_age'):
        detail.append('Age guidance: '+str(prefs['kids_age'])+'.')
    for w in shelves.read(s)['wishes']:
        if w['scope'] in (person, 'together'):
            sentence = 'Shelf '+w['name']+' for '+scope_label(s, w['scope'])+': '+w['text']
            if w['when'] != 'always':
                sentence += '; '+', '.join(w['when']['days'])+' from '+str(w['when']['hours'][0])+' to '+str(w['when']['hours'][1])+' '+s.CONFIG['region']['tz']
            detail.append(sentence+'.')
    wishes = Path(s.DATA)/'wishes.jsonl'
    if wishes.exists():
        for line in wishes.read_text().splitlines():
            if line.strip():
                wish = json.loads(line)
                if wish['who'] in (person, 'together'):
                    detail.append('Wishes for '+scope_label(s, wish['who'])+': '+wish['text']+'.')
    model = s.get_model(False) or {}
    facets = model.get('paff', {}).get(person, {})
    if facets:
        names = model.get('names', {})
        detail.append('Top taste: '+', '.join(names.get(k, k) for k in sorted(facets, key=lambda k: -facets[k])[:5])+'.')
    else:
        detail.append('Taste profile is still being built.')
    recent = journal(s).get(person, [])[-3:]
    if recent:
        detail.append('Recent commands: '+ '; '.join(e['name'] for e in recent)+'.')
    return '\n'.join(detail)
def context(s):
    scope = getattr(s._scoping, 'scope', None)
    return scope, s.SCOPES.get(scope), s._viewer.get() or s.who_watching()[0]
def confirm_code(plan):
    return f"{int(plan['plan'][:8], 16) % 100:02d}"
def preview_card(s, plan, stype):
    return card(s, 'yes '+confirm_code(plan)+': '+plan['name'], stype, 'Type "yes '+confirm_code(plan)+'" to apply. Nothing changes until then.'+('\n'+plan['description'] if plan.get('description') else ''))
def remember_cards(s, metas, scope):
    with _LOCK:
        cache = s.__dict__.setdefault('_command_cards', {})
        now = s.time.time()
        for key in list(cache):
            if now - cache[key][0] >= 1800:
                del cache[key]
        for meta in metas:
            if meta.get('id', '').startswith(s.rid('cmd:')):
                key = (scope, meta['id'])
                cache.pop(key, None)
                cache[key] = (now, copy.deepcopy(meta))
        while len(cache) > 200:
            del cache[next(iter(cache))]
def card_meta(s, cid, stype, scope):
    with _LOCK:
        cache = s.__dict__.setdefault('_command_cards', {})
        at, meta = cache.get((scope, cid), (0, None))
        if meta is not None and s.time.time() - at < 1800:
            return {'meta': {**copy.deepcopy(meta), 'type': stype}}
        cache.pop((scope, cid), None)
        return {'meta': {'id': cid, 'type': stype, 'name': 'Skald command',
                         'description': 'Search again to see this card.', 'poster': '/command.svg'}}
def once(s, scope, query, action):
    with _LOCK:
        now = s.time.time()
        cache = s.__dict__.setdefault('_command_results', {}).setdefault(scope, {})
        for key in list(cache):
            if now - cache[key][0] >= 60:
                del cache[key]
        query = query.strip().casefold()
        if query not in cache:
            result = action()
            if result is None:
                return None
            cache[query] = (now, copy.deepcopy(result))
        return copy.deepcopy(cache[query][1])
def suggestion_cards(s, scope, viewer, bound, stype, dismiss=None, *, text=''):
    from .engine import suggestions
    person = shelf_scope(s, None, viewer, bound)
    plans = s.__dict__.setdefault('_command_suggestions', {}).setdefault(scope, {})
    if dismiss:
        plan = plans.get(dismiss[3:])
        if not plan:
            raise ValueError('Preview suggestions again')
        suggestions(s.DATA, person, s.CONFIG, plan['suggestion'])
        del plans[dismiss[3:]]
        return card(s, 'Suggestion dismissed', stype)
    plans.clear()
    pending(s).pop(scope, None)
    metas = []
    from . import shelves
    for row in suggestions(s.DATA, person, s.CONFIG)['suggestions'][:2]:
        w = row['when']
        parts = []
        start, end = w['from_hour'], w['to_hour']+1
        if start < 5:
            parts.append({'days': [shelves.DAYS[(d-1)%7] for d in w['days']], 'hours': [start+24, min(end, 5)+24]})
        if end > 5:
            parts.append({'days': [shelves.DAYS[d] for d in w['days']], 'hours': [max(start, 5), end]})
        for when in parts:
            if len(metas) == 2:
                break
            if len([w for w in shelves.read(s)['wishes'] if w['scope'] == person and w['slot'] <= shelves.slot_count(s, person)]) >= shelves.slot_count(s, person):
                return error_card(s, ValueError('Shelves are full; stop a shelf first'), stype)
            name = re.sub(r'^(?:a|an|the|some)\s+', '', row['name'], flags=re.I).title()
            shelf = s.shelf_change(person, 'add', text=row['text'], name=name, when=when)
            plan = dict(people=[person], shelf=shelf, plan=shelf['plan'], suggestion=row['id'], at=s.time.time(), text=text[:200], name='Add shelf: '+name+' for '+s.LABELS[person]+'; '+str(shelf['when'])+' '+s.CONFIG['region']['tz'])
            code = confirm_code(plan)
            if code in plans:
                continue
            plans[code] = plan
            meta = preview_card(s, plan, stype)['metas'][0]
            meta['description'] += ' Type "no '+code+'" to dismiss.'
            metas.append(meta)
    return {'metas': metas} if metas else card(s, 'No suggestions yet', stype)

def special(s, query, stype, routed=False, results=None):
    with _LOCK:
        s.refresh_preferences()
        scope, bound, viewer = context(s)
        q = query.strip().casefold()
        mutate = q in ('undo', 'rückgängig') or bool(re.fullmatch(r'no \d{2}', q)) or (q.startswith('yes ') and len(q) == 6 and q[4:].isdigit())
        action = lambda: _special(s, query, stype, routed, results)
        result = once(s, scope, q, action) if q in ('help', '?', 'hilfe') or mutate and bound != s.GUEST and viewer != s.GUEST else action()
        if result is not None and stype != 'movie':
            return {'metas': []}
        for meta in (result or {}).get('metas', []):
            meta['type'] = stype
        return result
def _special(s, query, stype, routed=False, results=None):
    scope, bound, viewer = context(s)
    q = query.casefold().strip()
    help_q = q in ('help', '?', 'hilfe')
    ideas = q in ('ideas', 'suggest')
    local = None if results else parse_local(query, s, viewer, bound)['op']
    if not results and (q.startswith('stop ') and not local or REGISTRY.get(local, {}).get('group') == 'search'):
        return None
    dismissing = bool(re.fullmatch(r'no \d{2}', q))
    named = False if results else parse_about(query, s)
    about = not results and (named is not False or q in ('my settings', 'settings'))
    named = named if named is not False else None
    match = None if about or results else (re.fullmatch(r'(?:my )?settings(?: for)? (.+)', q) or re.fullmatch(r"(.+?)(?:'s)? (?:my )?settings", q))
    if match:
        about, named = True, member_name(s, match[1])
    confirming = q in ('y', 'ye', 'yes', 'ja', 'ok') or (q.startswith('yes ') and q[4:].isdigit())
    if results and not routed and not (help_q or ideas or dismissing or confirming or q in ('undo', 'rückgängig')):
        return None
    if not (help_q or ideas or dismissing or local or about or routed or confirming or q in ('undo', 'rückgängig')):
        return None
    try:
        if not help_q and (bound == s.GUEST or viewer == s.GUEST):
            raise PermissionError('Ask a grown-up to change settings')
        if ideas or dismissing:
            return suggestion_cards(s, scope, viewer, bound, stype, q if dismissing else None, text=query)
        if help_q:
            phrases = ('hide anime', 'no more horror', 'every sunday morning a classic for us', 'make me a row of heist films', 'my settings')
            result = card(s, 'Skald help', stype, '; '.join(phrases)+'. Type the exact yes code from the preview to apply; plain yes changes nothing. '+('Applies to '+s.LABELS[bound]+'.' if bound in s.PEOPLE else 'Add a member name, or choose Who\'s watching.'))
            for phrase in phrases:
                result['metas'].extend(card(s, 'Try: '+phrase, stype, 'Type "'+phrase+'" in search.')['metas'])
            return result
        if about:
            person = shelf_scope(s, named, viewer, bound)
            return card(s, 'About '+scope_label(s, person), stype, about_detail(s, person))
        plan = get_pending(s, scope)
        if confirming and q.startswith('yes '):
            plan = getattr(s, '_command_suggestions', {}).get(scope, {}).get(q[4:], plan)
            if plan and plan.get('suggestion') and s.time.time() - plan['at'] >= 600:
                raise ValueError('Preview suggestions again')
        if confirming:
            if plan is None:
                return None
            if q != 'yes '+confirm_code(plan):
                return preview_card(s, plan, stype)
            own = 'shelf' in plan or plan.get('wish') or plan.get('rule')
            if own:
                shelf_scope(s, plan['shelf']['scope'] if 'shelf' in plan else plan['people'][0], viewer, bound)
            elif bound in s.PEOPLE and plan['people'] != [bound] and not (plan.get('reset') or plan.get('target')):
                raise PermissionError('This plan applies to another member')
            if not own:
                if plan.get('reset') or plan.get('target'):
                    for target in plan['people']:
                        shelf_scope(s, target, viewer, bound)
                    if bound in s.PEOPLE and any(p != bound for p in plan['before']):
                        raise PermissionError('This plan applies to another member')
                    plan['actor'] = bound if bound in s.PEOPLE else viewer
                elif plan.get('change', {}).get('op') in ('undo', 'kids_age'):
                    people(s, viewer=viewer, bound=bound)
                    for target in plan['people']:
                        shelf_scope(s, target, viewer, bound)
                else:
                    for target in plan['people']:
                        people(s, viewer=target, bound=bound)
            result = apply(s, plan)
            pending(s).pop(scope, None)
            getattr(s, '_command_suggestions', {}).pop(scope, None)
            if not plan.get('wish'):
                s._commands_last = getattr(s, '_commands_last', {}) | {scope: plan['people']}
            return card(s, result['name'], stype)
        if q in ('undo', 'rückgängig'):
            targets = getattr(s, '_commands_last', {}).get(scope) or ([bound] if bound in s.PEOPLE else None) or people(s, viewer=viewer)
            for target in targets:
                shelf_scope(s, target, viewer, bound)
            return card(s, undo(s, targets, actor=viewer)['name'], stype)
        prior = get_pending(s, scope)
        pending(s).pop(scope, None)
        getattr(s, '_command_suggestions', {}).pop(scope, None)
        plan = preview(s, query, viewer, bound, scope=scope, prior=prior, results=results)
        if plan is not None and plan.get('search'):
            return None
        if plan is None:
            return card(s, 'Skald cannot set this yet', stype)
        if plan.get('read_only'):
            return card(s, plan['name'], stype, plan['description'])
        pending(s)[scope] = (s.time.time(), plan)
        # A new plan may reuse a two-digit code, so earlier "yes NN" or "undo" replies must not be replayed for it.
        getattr(s, '_command_results', {}).pop(scope, None)
        return preview_card(s, plan, stype)
    except (ValueError, PermissionError) as e:
        return error_card(s, e, stype, query)
def api_apply(s, req):
    with _LOCK:
        s.refresh_preferences()
        viewer = req['viewer']
        if viewer == s.GUEST:
            raise PermissionError('person')
        scope = 'api:'+req['client']['name']
        if req['body'] == {'undo': True}:
            targets = getattr(s, '_commands_last', {}).get(scope) or ([viewer] if viewer else [])
            for target in targets:
                shelf_scope(s, target, viewer, viewer)
        return once(s, scope, json.dumps(req['body'], sort_keys=True), lambda: _api_apply(s, req))
def _api_apply(s, req):
    viewer, body = req['viewer'], req['body']
    if viewer == s.GUEST:
        raise PermissionError('person')
    scope = 'api:'+req['client']['name']
    if body == {'undo': True}:
        targets = getattr(s, '_commands_last', {}).get(scope) or ([viewer] if viewer else [])
        for target in targets:
            shelf_scope(s, target, viewer, viewer)
        return undo(s, targets)
    plan = get_pending(s, scope)
    if not isinstance(body, dict) or set(body) != {'plan'} or not plan or body['plan'] != plan['plan']:
        raise ValueError('Preview again')
    if 'shelf' in plan:
        shelf_scope(s, plan['shelf']['scope'], viewer, viewer)
    elif plan.get('reset') or plan.get('target'):
        for target in plan['people']:
            shelf_scope(s, target, viewer, viewer)
        if viewer in s.PEOPLE and any(p != viewer for p in plan['before']):
            raise PermissionError('person')
        plan['actor'] = viewer or plan['actor']
    elif s.kids_age(viewer) is not None:
        raise PermissionError('person')
    elif viewer and plan['people'] != [viewer]:
        raise PermissionError('person')
    result = apply(s, plan)
    pending(s).pop(scope, None)
    s._commands_last = getattr(s, '_commands_last', {}) | {scope: plan['people']}
    return result
def api_preview(s, req):
    s.refresh_preferences()
    body, viewer = req['body'], req['viewer']
    if viewer == s.GUEST:
        raise PermissionError('person')
    if not isinstance(body, dict) or set(body) - {'in', 'person', 'no_llm'} or not isinstance(body.get('in'), dict) or set(body['in']) != {'query'}:
        raise ValueError('body')
    person = body.get('person', viewer)
    if viewer and person != viewer:
        raise PermissionError('person')
    if type(body.get('no_llm', False)) is not bool:
        raise ValueError('no_llm')
    s.check({'query': str}, body['in'])
    local = parse_local(body['in']['query'], s, person, viewer)
    if s.kids_age(viewer) is not None and not REGISTRY.get(local['op'], {}).get('kids'):
        raise PermissionError('person')
    scope = 'api:'+req['client']['name']
    prior = get_pending(s, scope)
    pending(s).pop(scope, None)
    answer = local if local['op'] else s.decide('parse_command', body['in'], allow_llm=not body.get('no_llm', False))['answer']
    plan = preview(s, body['in']['query'], person, viewer, answer=answer, scope=scope, prior=prior)
    if plan and not plan.get('read_only'):
        plan['source'] = 'api'
        pending(s)[scope] = (s.time.time(), plan)
    return {'answer': answer, 'preview': plan}
