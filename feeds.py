# =============================================================================
# project-squirrel -- feeds.py
#
# Loader for feeds.yml, the project feed registry (issue #270): one YAML file
# declares every video/audio feed in the house and which consumers may use
# it, and this module is the one place it is parsed. Consumers dispatch on a
# feed's `kind`, never its name -- the registry exists so no source name is
# hardcoded again (listener/earl.py's source_commands() branched on
# "amcrest" until #270, which made every new feed a code edit).
#
# Fail-loud on purpose (the env_float ethos): a malformed registry raises at
# import of the config, at startup -- never runs half-configured while
# looking healthy. Malformations that raise: unreadable file, invalid YAML,
# duplicate feed names (PyYAML's default is to silently keep the last one --
# the strict loader below exists because a silent overwrite is exactly how a
# copy-pasted camera block eats its neighbor), unknown kind, a kind missing
# its required field, unknown keys (typo'd flags must not read as false),
# non-boolean flags, and more than one naturalist feed.
#
# Import cost: yaml only. `yaml` sits on every venv in the fleet including
# the Pi's two-package one (test_import_boundary.py's PI_DEPS) -- the one
# venv that needs a deploy step is Earl's separate py3.11 venv on pearl
# (Servers/Pearl.md).
# =============================================================================

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent / "feeds.yml"

# One flag per consumer. A future consumer (a transcriber, a second vision
# daemon) adds its flag here and in the Feed fields -- nowhere else.
CONSUMERS = ("earl", "naturalist")

# What each kind must carry. A new kind (the rover's MJPEG video, #236) is
# an entry here plus dispatch in the consumers that can use it.
REQUIRED_BY_KIND = {"rtsp": "url", "command": "cmd"}

_ALLOWED_KEYS = {"kind", "url", "cmd"} | set(CONSUMERS)


class FeedsError(RuntimeError):
    """A malformed feeds registry. The message names the file, the feed,
    and the exact complaint -- startup is the time to be loud."""


@dataclass(frozen=True)
class Feed:
    name: str
    kind: str
    url: str = None
    cmd: str = None
    earl: bool = False
    naturalist: bool = False


def redact_rtsp(url):
    """rtsp://user:pass@host/... -> rtsp://user:***@host/... A restream URL
    carries no credentials and passes through unchanged; the mask exists so
    a direct camera URL in an alternate registry (or MERLE_RTSP_URL) can
    never leak into logs. Deliberately a twin of vision/frames.py's
    redact_rtsp rather than an import: frames.py needs cv2, and this module
    is imported by the lean listener package (the ENH_SUFFIX precedent)."""
    return re.sub(r"(?<=//)([^/@:]+):[^@/]+@", r"\1:***@", url)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys instead of silently
    keeping the last -- two feeds under one name is a config bug, not a
    preference for whichever was pasted lower."""


def _no_duplicate_keys(loader, node):
    seen = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise FeedsError(f"duplicate key {key!r} in the feeds registry")
        seen.add(key)
    return loader.construct_mapping(node)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def registry_path():
    """Where the registry lives: MERLE_FEEDS when set (tests, dev variants,
    the break-glass direct-camera file), else feeds.yml beside this module --
    path-of-this-file rather than CWD so every unit and tool agrees."""
    override = os.environ.get("MERLE_FEEDS", "").strip()
    return Path(override) if override else DEFAULT_PATH


def _build(path, name, spec):
    where = f"{path}: feed {name!r}"
    if not isinstance(spec, dict):
        raise FeedsError(f"{where} must be a mapping, got {type(spec).__name__}")
    unknown = set(spec) - _ALLOWED_KEYS
    if unknown:
        raise FeedsError(f"{where} has unknown keys {sorted(unknown)} "
                         f"(known: {sorted(_ALLOWED_KEYS)}) -- a typo'd flag "
                         "must not silently read as false")
    kind = spec.get("kind")
    if kind not in REQUIRED_BY_KIND:
        raise FeedsError(f"{where} has kind {kind!r} "
                         f"(known: {sorted(REQUIRED_BY_KIND)})")
    required = REQUIRED_BY_KIND[kind]
    value = spec.get(required)
    if not isinstance(value, str) or not value.strip():
        raise FeedsError(f"{where} is kind {kind!r} and needs a non-empty "
                         f"{required!r}")
    for flag in CONSUMERS:
        if flag in spec and not isinstance(spec[flag], bool):
            raise FeedsError(f"{where}: {flag!r} must be true or false, "
                             f"got {spec[flag]!r}")
    return Feed(name=name, kind=kind,
                url=spec.get("url"), cmd=spec.get("cmd"),
                **{flag: bool(spec.get(flag, False)) for flag in CONSUMERS})


def load_feeds(path=None):
    """The registry -> {name: Feed}, in file order, validated. Raises
    FeedsError on anything malformed -- see the module header for the list."""
    path = Path(path) if path is not None else registry_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise FeedsError(f"feeds registry unreadable: {path} ({e})")
    try:
        data = yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as e:
        raise FeedsError(f"{path} is not valid YAML: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("feeds"), dict) \
            or not data["feeds"]:
        raise FeedsError(f"{path}: expected a top-level 'feeds:' mapping "
                         "with at least one feed")
    feeds = {name: _build(path, name, spec)
             for name, spec in data["feeds"].items()}
    naturalists = [f.name for f in feeds.values() if f.naturalist]
    if len(naturalists) > 1:
        raise FeedsError(f"{path}: {len(naturalists)} feeds are flagged "
                         f"naturalist ({naturalists}) -- the vision daemon "
                         "has exactly one video source")
    return feeds


def feed(name, path=None):
    """One feed by name, or FeedsError if absent -- for a consumer that wants
    a specific feed by name (the daemon's house-front eye, #274) rather than
    by consumer flag."""
    loaded = load_feeds(path)
    if name not in loaded:
        raise FeedsError(f"{registry_path() if path is None else path}: "
                         f"no feed named {name!r} (have {sorted(loaded)})")
    return loaded[name]


def feeds_for(consumer, path=None):
    """Every feed flagged for `consumer`, in file order. The consumer name
    is checked against CONSUMERS so a typo here fails loud too."""
    if consumer not in CONSUMERS:
        raise FeedsError(f"unknown consumer {consumer!r} "
                         f"(known: {sorted(CONSUMERS)})")
    return [f for f in load_feeds(path).values() if getattr(f, consumer)]


def feed_for(consumer, path=None):
    """The single feed flagged for `consumer` -- for consumers that take
    exactly one (the naturalist). Zero is as loud as two."""
    matches = feeds_for(consumer, path)
    if len(matches) != 1:
        raise FeedsError(f"{registry_path() if path is None else path}: "
                         f"expected exactly one feed flagged {consumer!r}, "
                         f"found {[f.name for f in matches] or 'none'}")
    return matches[0]
