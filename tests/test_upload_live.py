"""The admin page's Uploads tab shows "Live" once Breeze can use an upload's document (user, 2026-11-01). The admin
login (eq_admin) reads nothing in kb, so control.upload_live says it per upload: true only for a document that is live
and not hidden. When the crawler refused to put it live, held_reason says why ("Held back", user 2026-11-01).

Local library in a tmp folder (DOCS_DIR, inbox/ inside it), `_testing`, one rolled-back transaction per test."""
from p11support import world  # noqa: F401  (world is a fixture)
from p3support import q
from test_uploads import a_pdf, outcome, process, queue


def live(conn, upload):
    return q(conn, "SELECT live FROM control.upload_live WHERE upload_id = %s", (upload,))


def held(conn, upload):
    return q(conn, "SELECT held_reason FROM control.upload_live WHERE upload_id = %s", (upload,))[0][0]


def test_an_upload_is_live_only_once_its_document_is_live_and_shown(world):
    upload = queue(world.conn, a_pdf("Model RA15AZ24AJ3CA"), brand=world.brand)
    assert live(world.conn, upload) == ()  # queued: no document yet
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    assert live(world.conn, upload) == ((False,),)  # added, not yet tagged and built
    q(world.conn, "UPDATE documents SET state = 'live' WHERE id = %s", (document_id,))
    assert live(world.conn, upload) == ((True,),)
    q(world.conn, "UPDATE documents SET hidden_at = now() WHERE id = %s", (document_id,))
    assert live(world.conn, upload) == ((False,),)


def test_the_admin_login_can_read_it_and_nothing_more(migrate_conn):
    rights = q(migrate_conn, "SELECT p FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p "
                             "WHERE has_table_privilege('eq_admin', 'control.upload_live', p)")
    assert rights == (("SELECT",),)
    columns = q(migrate_conn, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'control' "
                              "AND table_name = 'upload_live' ORDER BY ordinal_position")
    assert columns == (("upload_id",), ("live",), ("held_reason",))


def test_an_upload_the_crawler_refused_to_put_live_says_why_until_it_goes_live(world):
    from crawler import kb
    upload = queue(world.conn, a_pdf("Model RA15AZ24AJ3CA"), brand=world.brand)
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    q(world.conn, "UPDATE documents SET state = 'tagged', doc_type = 'not_equipment' WHERE id = %s", (document_id,))
    kb.build(world.conn, document_id)
    assert kb.publish(world.conn, document_id) == (False, "the document is not about equipment")
    assert live(world.conn, upload) == ((False,),)
    assert held(world.conn, upload) == "the document is not about equipment"
    q(world.conn, "UPDATE documents SET doc_type = 'service_manual' WHERE id = %s", (document_id,))
    assert kb.publish(world.conn, document_id) == (True, None)
    assert live(world.conn, upload) == ((True,),)
    assert held(world.conn, upload) is None
