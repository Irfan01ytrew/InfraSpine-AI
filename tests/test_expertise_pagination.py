from sqlalchemy import text
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db
from tests.test_expertise import _department, _add_member


def test_expertise_pagination_rechecks_department_scope(client):
    login(client, ADMIN_EMAIL)
    dept = _department(client, 'Pagination synthetic department')
    _add_member(client, dept['id'], USER_EMAIL)
    async def seed(conn):
        await conn.execute(text("""INSERT INTO users (id,email,password_hash,role,expertise_tags)
            SELECT gen_random_uuid(), 'page-' || lpad(i::text, 3, '0') || '@example.test', 'disabled', 'employee', '[\"synthetic skill\"]'::jsonb FROM generate_series(1,52) i"""))
        await conn.execute(text("""INSERT INTO access_group_members (group_id,user_id)
            SELECT CAST(:group_id AS uuid),id FROM users WHERE email LIKE 'page-%@example.test'"""), {'group_id':dept['id']})
    run_db(seed)
    login(client, USER_EMAIL)
    first = client.get('/api/expertise/search', params={'skill':'synthetic skill'})
    assert first.status_code == 200
    page = first.json()
    assert len(page['matches']) == 50 and page['next_cursor'] == 'page-050@example.test'
    login(client, ADMIN_EMAIL)
    user = next(x for x in client.get('/api/admin/users').json()['users'] if x['email']=='page-051@example.test')
    assert client.delete(f"/api/admin/access/groups/{dept['id']}/members/{user['id']}").status_code == 204
    login(client, USER_EMAIL)
    second = client.get('/api/expertise/search', params={'skill':'synthetic skill','after':page['next_cursor']})
    assert second.status_code == 200
    assert [x['email'] for x in second.json()['matches']] == ['page-052@example.test']
    assert second.json()['next_cursor'] is None
    assert client.get('/api/expertise/search', params={'skill':'synthetic skill','after':'x'*321}).status_code == 422
