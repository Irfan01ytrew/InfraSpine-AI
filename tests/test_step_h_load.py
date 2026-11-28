"""Step H slices 4-5 (bounded): load harness on the real app + ingestion projection.

Slice 4 — a small-scale load pass against the real app in-process (ASGI
transport, no network, no server): N virtual users x M iterations over
health / login / documents / founder_note. It proves the harness, the
transport and the founder intake path under concurrency, and reports
p50/p95/p99 + error rate. Scale it with STEP_H_LOAD_USERS /
STEP_H_LOAD_ITERATIONS (defaults 5 / 2). The 1,000-user qualification itself
needs a staging stack on target-like hardware and is NOT claimed here.

Slice 5 — the ingestion projection is measured, not guessed: the real
``split_text`` chunker is timed over a deterministic synthetic corpus, and
the time-to-index for 600 GB is projected from that measurement with its
assumptions stated. Chunking is the cheapest ingestion stage, so the
projection is a lower bound — parsing, embedding and index writes are not
in it.
"""

import asyncio
import os

import pytest
from sqlalchemy import text

from app.qa.asgi_load import AsgiTransport
from app.qa.ingestion_projection import (
    Projection,
    Throughput,
    measure_chunk_throughput,
    project_time_to_index,
    synthetic_corpus,
)
from app.qa.load_harness import LoadScenario, LoadStep, run_load_pass


# --- Slice 5: projection (pure) ------------------------------------------------

def test_synthetic_corpus_is_deterministic_and_pages_differ():
    first = synthetic_corpus(pages=10, page_bytes=1024)
    second = synthetic_corpus(pages=10, page_bytes=1024)
    assert first == second
    assert len(first) == 10
    assert len(set(first)) == 10
    assert all(900 <= len(page) <= 1100 for page in first)


def test_measure_chunk_throughput_times_the_real_chunker():
    corpus = synthetic_corpus(pages=20, page_bytes=4096)
    measured = measure_chunk_throughput(corpus)
    assert measured.files == 20
    assert measured.bytes_total == sum(len(page) for page in corpus)
    assert measured.seconds > 0
    assert measured.mb_per_second > 0


def test_projection_linear_math_is_exact():
    measured = Throughput(bytes_total=5 * 1024 * 1024, seconds=1.0, files=1)
    projection = project_time_to_index(measured)
    # 600 GiB at 5 MiB/s = 122,880 s.
    assert projection.seconds == pytest.approx(600 * 1024 / 5, rel=1e-9)
    assert projection.hours == pytest.approx(34.13, abs=0.01)
    assert projection.model == 'linear'
    assert any('lower bound' in note for note in projection.assumptions)


def test_projection_sublinear_is_slower_than_linear():
    measured = Throughput(bytes_total=5 * 1024 * 1024, seconds=1.0, files=1)
    linear = project_time_to_index(measured, model='linear')
    slower = project_time_to_index(measured, model='sublinear', scaling_exponent=0.9)
    assert slower.seconds > linear.seconds
    assert slower.assumptions != linear.assumptions


def test_projection_rejects_bad_inputs():
    with pytest.raises(ValueError):
        project_time_to_index(Throughput(bytes_total=0, seconds=0.0, files=0))
    with pytest.raises(ValueError):
        project_time_to_index(Throughput(bytes_total=100, seconds=1.0, files=1), model='magic')
    with pytest.raises(ValueError):
        project_time_to_index(Throughput(bytes_total=100, seconds=1.0, files=1),
                              model='sublinear', scaling_exponent=0)


def test_measured_projection_for_600_gb_is_recorded_with_assumptions():
    corpus = synthetic_corpus(pages=50, page_bytes=4096)
    measured = measure_chunk_throughput(corpus)
    projection = project_time_to_index(measured)
    assert isinstance(projection, Projection)
    assert projection.target_bytes == 600 * 1024 * 1024 * 1024
    assert projection.seconds > 0
    assert projection.assumptions, 'a projection without assumptions is a guess'


# --- Slice 4: ASGI transport against a fake app (pure) ---------------------------

async def _fake_asgi_app(scope, receive, send):
    assert scope['type'] == 'http'
    body = b'{"ok": true}'
    if scope['path'] == '/api/auth/login':
        body = b'{"token": "fake"}'
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'application/json')]})
    await send({'type': 'http.response.body', 'body': body})


def test_asgi_transport_drives_each_step_and_rejects_unknown():
    transport = AsgiTransport(_fake_asgi_app, email='a@b.c', password='x', scope_id='s')
    asyncio.run(transport(LoadStep('health'), 0))
    asyncio.run(transport(LoadStep('login'), 0))
    asyncio.run(transport(LoadStep('documents'), 0))
    asyncio.run(transport(LoadStep('founder_note'), 0))
    with pytest.raises(ValueError, match='unknown load step'):
        asyncio.run(transport(LoadStep('explode'), 0))
    asyncio.run(transport.aclose())


# --- Slice 4: small real-app load pass (throwaway DB) -----------------------------

def test_small_real_app_load_pass(client):
    # Imported here, not at module level: importing the app needs the test
    # environment conftest sets up (DATABASE_URL / MASTER_ENCRYPTION_KEY).
    from app.main import app
    from tests.conftest import ADMIN_EMAIL, PASSWORD, login, run_db
    from tests.test_enterprise_people import _department

    login(client, ADMIN_EMAIL)
    group = _department(client, 'Load QA')

    async def admin_id(conn):
        return (await conn.execute(
            text("SELECT id FROM users WHERE email = :e"), {'e': ADMIN_EMAIL})).scalar()

    founder = run_db(admin_id)
    run_db(lambda c: c.execute(
        text("INSERT INTO access_group_members (group_id,user_id) VALUES (:g,:u)"),
        {'g': group['id'], 'u': founder}))
    scope_id = group['id']

    users = int(os.environ.get('STEP_H_LOAD_USERS', '5'))
    # 6 = one full weighted cycle (health x2, login, documents x2, founder_note).
    iterations = int(os.environ.get('STEP_H_LOAD_ITERATIONS', '6'))
    transport = AsgiTransport(app, email=ADMIN_EMAIL, password=PASSWORD, scope_id=scope_id)
    scenario = LoadScenario(
        name='step-h-small-scale', virtual_users=users, iterations_per_user=iterations,
        steps=(LoadStep('health', 2), LoadStep('login', 1),
               LoadStep('documents', 2), LoadStep('founder_note', 1)))
    try:
        report = asyncio.run(run_load_pass(scenario, transport))
    finally:
        asyncio.run(transport.aclose())
    assert report.requests == users * iterations
    assert report.errors == 0, f'{report.errors} errors in a {report.requests}-request pass'
    assert report.error_rate == 0
    assert report.p50_ms is not None and report.p95_ms is not None and report.p99_ms is not None
    assert report.throughput_rps is not None and report.throughput_rps > 0
