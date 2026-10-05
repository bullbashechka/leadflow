"""Rehearse PostgreSQL dump/restore using isolated synthetic local databases only."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEED = """
import django
django.setup()
import uuid
from django.utils import timezone
from leadflow.crm.models import Tag
from leadflow.crm.services import create_lead, delete_lead
from leadflow.bot.models import BotPollingState, ProcessedUpdate, OutboundMessage
payload = {'name': 'Synthetic restore rehearsal', 'request': 'Synthetic text',
    'contacts': ['restore@example.com'], 'tag_ids': [Tag.objects.get(code='website').pk]}
create_lead(uuid.UUID(int=1), payload)
deleted = create_lead(uuid.UUID(int=2), payload).lead
delete_lead(deleted.pk, {'operation_id': str(uuid.UUID(int=3)), 'expected_version': 1})
polling = BotPollingState.objects.create(bot_id=1)
update = ProcessedUpdate.objects.create(polling_state=polling, update_id=1)
OutboundMessage.objects.create(processed_update=update, ordinal=0, chat_id=1,
    text='Synthetic restore delivery', next_attempt_at=timezone.now())
"""

CHECK = """
import django
django.setup()
import json
import uuid
from django.db import connection
from leadflow.crm.models import CRMState, Lead, LeadContact, SubmissionReceipt, MutationReceipt
from leadflow.crm.services import create_lead, delete_lead, LeadDeleted
from leadflow.bot.models import OutboundMessage
from django.db.migrations.recorder import MigrationRecorder
lead = Lead.objects.get()
assert lead.name == 'Synthetic restore rehearsal'
assert LeadContact.objects.get().value == 'restore@example.com'
assert list(lead.tags.values_list('code', flat=True)) == ['website']
assert CRMState.objects.get(pk=1).last_arrival_sequence == 2
assert SubmissionReceipt.objects.filter(lead=lead).count() == 1
assert SubmissionReceipt.objects.filter(lead=None, payload={}).count() == 1
receipt = SubmissionReceipt.objects.get(submission_id=uuid.UUID(int=1))
assert create_lead(receipt.pk, receipt.payload).replayed
payload = {'name': 'Synthetic restore rehearsal', 'request': 'Synthetic text',
    'contacts': ['restore@example.com'], 'tag_ids': list(lead.tags.values_list('pk', flat=True))}
try:
    create_lead(uuid.UUID(int=2), payload)
except LeadDeleted:
    pass
else:
    raise AssertionError('Deleted submission was resurrected')
deleted_id = SubmissionReceipt.objects.get(submission_id=uuid.UUID(int=2)).deleted_lead_id
mutation = MutationReceipt.objects.get()
assert mutation.target_id == str(deleted_id)
assert delete_lead(deleted_id,
    {'operation_id': str(uuid.UUID(int=3)), 'expected_version': 1}).replayed
message = OutboundMessage.objects.get()
assert message.status == 'pending' and message.text == 'Synthetic restore delivery'
with connection.cursor() as cursor:
    cursor.execute('SELECT count(*) FROM pg_constraint WHERE connamespace = '
        '(SELECT oid FROM pg_namespace WHERE nspname = %s)', ['public'])
    constraints = cursor.fetchone()[0]
print(json.dumps({'leads': Lead.objects.count(), 'contacts': LeadContact.objects.count(),
    'submission_receipts': SubmissionReceipt.objects.count(),
    'mutation_receipts': MutationReceipt.objects.count(),
    'pending_messages': OutboundMessage.objects.filter(status='pending').count(),
    'migrations': MigrationRecorder.Migration.objects.count(), 'constraints': constraints},
    sort_keys=True))
"""


def compose(*args, input=None, output=None):
    # Refuse inherited selectors that could direct this local-only exercise to a remote daemon.
    environment = os.environ.copy()
    for name in (
        "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    ):
        environment.pop(name, None)
    executable = shutil.which("docker")
    if not executable:
        raise RuntimeError("Docker is required for the local restore rehearsal.")
    # All arguments come from this script; database names are generated UUID identifiers.
    result = subprocess.run(  # noqa: S603
        [executable, "--host=unix:///var/run/docker.sock", "compose", *args], cwd=ROOT,
        env=environment, input=input, stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        # Database tooling can include connection details in stderr. Do not print them.
        raise RuntimeError("Local restore rehearsal failed; inspect local services privately.")
    return result.stdout


def postgres(tool, *args, input=None, output=None):
    return compose(
        "exec", "-T", "postgres", "sh", "-c", 'exec "$@" -U "$POSTGRES_USER"',
        "sh", tool, *args, input=input, output=output,
    )


def python(database, code):
    return compose(
        "exec", "-T", "-e", "DATABASE_URL=", "-e", f"POSTGRES_DB={database}",
        "-e", "POSTGRES_HOST=postgres", "-e", "POSTGRES_PORT=5432",
        "-e", "DJANGO_SETTINGS_MODULE=config.settings.local",
        "api", "python", "-B", "-c", code,
    )


def run():
    identifier = uuid.uuid4().hex
    source = f"leadflow_restore_source_{identifier}"
    restored = f"leadflow_restore_target_{identifier}"
    created = []
    try:
        for database in (source, restored):
            postgres("createdb", "--maintenance-db=postgres", database)
            created.append(database)
        python(source, "import subprocess; subprocess.run("
               "['python', 'manage.py', 'migrate', '--noinput'], check=True)")
        python(source, SEED)
        before = json.loads(python(source, CHECK))
        with tempfile.TemporaryFile() as archive:
            postgres("pg_dump", "--format=custom", "--no-owner", "--no-acl", source,
                     output=archive)
            archive.seek(0)
            # subprocess input must be bytes. The archive contains only this synthetic fixture.
            postgres("pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
                     "--dbname", restored, input=archive.read())
        after = json.loads(python(restored, CHECK))
        if before != after:
            raise RuntimeError("Restored aggregate counts do not match the source fixture.")
        print(json.dumps({"status": "ok", "synthetic_only": True, "restored": after},
                         sort_keys=True))
    finally:
        cleanup_failed = False
        for database in reversed(created):
            try:
                postgres("dropdb", "--maintenance-db=postgres", "--force", database)
            except RuntimeError:
                cleanup_failed = True
        if cleanup_failed:
            raise RuntimeError("Could not clean up the isolated restore rehearsal databases.")


if __name__ == "__main__":
    try:
        run()
    except (RuntimeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
