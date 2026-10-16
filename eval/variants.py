"""Small scoring registry: a factory receives Context and returns score(key, person=None)."""
from dataclasses import dataclass
from contextlib import contextmanager
from functools import cache
import hashlib
import math
import random
import re


@contextmanager
def experience_overlay(server, overlay):
    """Add only missing, kind-checked tags for one offline arm, then restore its inputs."""
    if not isinstance(overlay, dict):
        raise ValueError('experience overlay must be a JSON object')
    facets = dict(server._facets)
    for key, record in overlay.items():
        if not isinstance(record, dict):
            raise ValueError(f'invalid experience overlay record: {key}')
        kind, tags = record.get('kind'), record.get('x')
        if (kind not in ('movie', 'tv') or key not in facets or facets[key].get('kind') != kind
                or server.fkey(key.split('@')[0], kind) != key):
            raise ValueError(f'overlay key/kind is not in the frozen universe: {key}')
        if 'x' in facets[key] or set(record) != {'kind', 'x'} or not isinstance(tags, dict) or not tags:
            raise ValueError(f'overlay must add missing experience tags only: {key}')
        if any(tag not in server.EXPERIENCES or type(value) is not int or not 1 <= value <= 3
               for tag, value in tags.items()):
            raise ValueError(f'invalid experience tags: {key}')
        facets[key] = {**facets[key], 'x': dict(tags)}
    original, stats = server._facets, server._seed_stats
    try:
        server._facets = facets
        server.clear_seed_cache()
        yield
    finally:
        server._facets, server._seed_stats = original, stats
        server.clear_seed_cache()


@dataclass(frozen=True)
class Variant:
    name: str
    factory: object
    transform: object = None  # Optional transform(profile) before build_model; never mutate its input.
    tunable: bool = False


class Context:
    def __init__(self, server, model, profile, seed):
        self.server, self.model, self.profile, self.seed = server, model, profile, seed
        # Per-model caches die with this context rather than retaining every LOO model.
        for name in ('fit', 'bonus', 'x_profile', 'x'):
            setattr(self, name, cache(getattr(self, name)))

    def fit(self, key, person=None):
        m = self.model
        vec = m['pvec'].get(person) if person else m['vec']
        return self.server.fit(vec, key)

    def bonus(self, key, person=None):
        rec = self.server._facets.get(key)
        if not rec:
            return None
        aff = self.model['paff'].get(person) if person else None
        return self.server.facet_bonus(self.model, rec, aff)[0]

    def x_profile(self, person=None):
        if self.model and 'x' in self.model:
            return self.model['px'].get(person, {}) if person else self.model['x']
        entries = self.server.person_entries(self.profile, person) if person else self.profile['entries']
        acc = {}
        for e in entries:
            weight = e['weight'] * (0.5 if e['weight'] < 0 else 1.0)
            for tag, strength in sorted((self.server._facets.get(e['key'], {}).get('x') or {}).items()):
                acc[tag] = acc.get(tag, 0.0) + weight * strength
        return acc

    def x(self, key, person=None):
        tags = self.server._facets.get(key, {}).get('x')
        return self.server.xsim(tags, self.x_profile(person)) if tags else None


def current(ctx):
    return lambda key, person=None: ctx.server.score_key(ctx.model, key, person)[0]


def per_person(score):
    def evaluate(key, person=None):
        if person != 'together':
            return score(key, person)
        values = [score(key, pp) for pp in ('ana', 'ben')]
        if any(v is None for v in values):
            return None
        a, b = values
        return (a+b)/2 - .25*abs(a-b)
    return evaluate


def emb_only(ctx):
    return per_person(ctx.fit)


def facets_only(ctx):
    def score(key, person=None):
        value = ctx.bonus(key, person)
        return math.tanh(value) if value is not None else None
    return per_person(score)


def x_only(ctx):
    return per_person(ctx.x)


def random_floor(ctx):
    def score(key, person=None):
        digest = hashlib.sha256(f'{ctx.seed}:{key}'.encode()).digest()
        return random.Random(int.from_bytes(digest, 'big')).random()
    return score


def blend(a, b, c):
    def factory(ctx):
        def score(key, person=None):
            f, x, bonus = ctx.fit(key, person), ctx.x(key, person), ctx.bonus(key, person)
            if f is None and x is None and bonus is None:
                return None
            scale = ctx.model.get('pbscale', {}).get(person, 1.) if person else ctx.model['bscale']
            quality = ctx.server.quality_prior(ctx.server._facets.get(key)) if hasattr(ctx.server, 'quality_prior') else 0
            return a * (f or 0.0) + b * (x or 0.0) + c * math.tanh((bonus or 0.0) / scale) + quality
        return per_person(score)
    return Variant(f'blend({a:g},{b:g},{c:g})', factory, tunable=True)


REGISTRY = {v.name: v for v in [Variant('current', current), Variant('emb_only', emb_only),
                              Variant('facets_only', facets_only), Variant('x_only', x_only),
                              Variant('random', random_floor)]}
GRID = [blend(a, b, c) for a, b, c in [(1, 0, .12), (.75, .25, .12), (.5, .5, .12),
                                     (.25, .75, .12), (0, 1, .12), (.5, .5, 0)]]
GRID += [blend(a, b, c) for a in (0, .15, .3) for b in (0, .5, 1) for c in (.3, .6)]
REGISTRY['x_facets'] = Variant('x_facets', blend(0, .4, .6).factory)


def select(spec):
    # Commas inside blend(...) belong to its weights.
    names = re.split(r',\s*(?![^()]*\))', spec)
    selected, use_grid = [], False
    for name in names:
        name = name.strip()
        if name == 'grid':
            use_grid = True
            selected.extend(GRID)
        elif name in REGISTRY:
            selected.append(REGISTRY[name])
        else:
            match = re.fullmatch(r'blend\(([^()]+)\)', name)
            if not match:
                raise ValueError(f'unknown variant: {name}')
            weights = [float(x) for x in match[1].split(',')]
            if len(weights) != 3 or not all(math.isfinite(x) for x in weights):
                raise ValueError('blend requires three finite weights')
            selected.append(blend(*weights))
    return list({v.name: v for v in selected}.values()), use_grid
