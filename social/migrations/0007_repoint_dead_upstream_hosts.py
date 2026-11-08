# Repoint BufferAccount rows that still reference retired Render hostnames.
#
# The upstream news service moved from the old `ubuntureport`/`panafricanstates`
# hostnames to `panafricanreport`. Changing the field default in an already
# applied migration does not touch existing rows, so `fetch_recent` kept calling
# a hostname that Render reports as `x-render-routing: no-server` (HTTP 404).
from django.db import migrations

DEAD_HOSTS = (
    "ubuntureport.onrender.com",
    "panafricanstates.onrender.com",
)
LIVE_HOST = "panafricanreport.onrender.com"


def repoint_dead_hosts(apps, schema_editor):
    BufferAccount = apps.get_model("social", "BufferAccount")

    for account in BufferAccount.objects.all().only("id", "api_url"):
        url = account.api_url or ""
        new_url = url
        for host in DEAD_HOSTS:
            if host in new_url:
                new_url = new_url.replace(host, LIVE_HOST)

        if new_url != url:
            account.api_url = new_url
            account.save(update_fields=["api_url"])


def reverse_noop(apps, schema_editor):
    """No reverse: the retired hostnames do not serve the app anymore."""


class Migration(migrations.Migration):

    dependencies = [
        ("social", "0006_bufferaccount_api_url_bufferaccount_name"),
    ]

    operations = [
        migrations.RunPython(repoint_dead_hosts, reverse_noop),
    ]
