import fakeredis
import pytest
import redis

from app import schemas
from app.auth.service import AuthService
from app.mesh.cache import InMemoryCache, RedisCache
from app.mesh.factory import build_cache
from app.mesh.mesh import Principal, ServiceMesh
from app.mesh.transports import CachingTransport, InProcessTransport


@pytest.fixture
def redis_server():
    return fakeredis.FakeServer()


def redis_cache(server, **kwargs):
    return RedisCache(fakeredis.FakeRedis(server=server, decode_responses=True), **kwargs)


@pytest.fixture(params=["memory", "redis"])
def cache(request, redis_server):
    return InMemoryCache() if request.param == "memory" else redis_cache(redis_server)


# Contract: both implementations behave the same

def test_get_returns_what_was_set(cache):
    cache.set("k", "v", tags=["t"], ttl_seconds=60)

    assert cache.get("k") == "v"
    assert cache.get("missing") is None


def test_invalidating_a_tag_drops_only_its_entries(cache):
    cache.set("a", "1", tags=["meetings"], ttl_seconds=60)
    cache.set("b", "2", tags=["meetings", "notes"], ttl_seconds=60)
    cache.set("c", "3", tags=["users"], ttl_seconds=60)

    cache.invalidate_tags(["notes"])

    assert (cache.get("a"), cache.get("b"), cache.get("c")) == ("1", None, "3")


def test_clear_drops_everything(cache):
    cache.set("a", "1", tags=["t"], ttl_seconds=60)

    cache.clear()

    assert cache.get("a") is None


# Redis specifics

def test_redis_entries_and_tag_sets_expire(redis_server):
    cache = redis_cache(redis_server, prefix="p:")
    cache.set("k", "v", tags=["t"], ttl_seconds=30)
    raw = fakeredis.FakeRedis(server=redis_server)

    assert 0 < raw.ttl("p:entry:k") <= 30
    assert 0 < raw.ttl("p:tag:t") <= 30


def test_redis_ttl_zero_disables_caching(redis_server):
    cache = redis_cache(redis_server)

    cache.set("k", "v", tags=["t"], ttl_seconds=0)

    assert cache.get("k") is None


def test_redis_clear_only_touches_its_prefix(redis_server):
    other = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
    other.set("unrelated", "keep")
    cache = redis_cache(redis_server)
    cache.set("k", "v", tags=["t"], ttl_seconds=60)

    cache.clear()

    assert other.get("unrelated") == "keep"


def test_redis_is_shared_between_nodes(redis_server):
    node_a, node_b = redis_cache(redis_server), redis_cache(redis_server)
    node_a.set("k", "v", tags=["meetings"], ttl_seconds=60)

    assert node_b.get("k") == "v"
    node_b.invalidate_tags(["meetings"])
    assert node_a.get("k") is None


def test_redis_outage_degrades_reads_but_not_invalidations(redis_server):
    cache = redis_cache(redis_server)
    redis_server.connected = False

    assert cache.get("k") is None  # a miss: the operation still runs
    cache.set("k", "v", tags=["t"], ttl_seconds=60)  # not cached, no error
    with pytest.raises(redis.ConnectionError):
        cache.invalidate_tags(["t"])  # must not silently keep stale data


def test_build_cache_from_url():
    assert isinstance(build_cache(""), InMemoryCache)
    assert isinstance(build_cache("redis://localhost:6379/0"), RedisCache)
    with pytest.raises(ValueError):
        build_cache("memcached://localhost")


# Two mesh nodes, as in docker-compose.microservices.yml

def test_a_write_on_one_node_invalidates_reads_cached_on_another(db, redis_server):
    alice = Principal(AuthService(db).register(
        schemas.UserCreate(name="Alice", email="alice@example.com", password="password1")).id)

    def node(cache):
        return ServiceMesh(alice, CachingTransport(InProcessTransport(db), cache, ttl_seconds=60))

    shared_a, shared_b = node(redis_cache(redis_server)), node(redis_cache(redis_server))
    local_a, local_b = node(InMemoryCache()), node(InMemoryCache())
    assert shared_a.meetings.list() == local_a.meetings.list() == []  # cached on node A

    meeting = schemas.MeetingCreate(title="Standup", scheduled_time="2024-01-15T10:00:00")
    shared_b.meetings.create(data=meeting)
    local_b.meetings.create(data=meeting)

    assert len(shared_a.meetings.list()) == 2  # Redis: node B's write reached node A
    assert local_a.meetings.list() == []  # per-process caches: node A is stale
