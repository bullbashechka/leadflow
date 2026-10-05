from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from leadflow.crm.models import CRMState
from leadflow.crm.models import Lead
from leadflow.crm.models import LeadContact
from leadflow.crm.models import Tag
from leadflow.crm.validation import validate_contacts

DEMO_LEADS = [
    {
        "name": "Демо: Анна Миронова",
        "contact": "anna.demo@example.com",
        "request": "Нужен сайт-каталог для вымышленной мастерской.",
        "source": Lead.Source.TELEGRAM_BOT,
        "status": Lead.Status.NEW,
        "tags": ("website",),
    },
    {
        "name": "Демо: Борис Лебедев",
        "contact": "boris.demo@example.com",
        "request": "Обсудить настройку рекламы для учебного проекта.",
        "source": Lead.Source.MANUAL,
        "status": Lead.Status.IN_PROGRESS,
        "tags": ("advertising",),
    },
    {
        "name": "Демо: Вера Соколова",
        "contact": "vera.demo@example.com",
        "request": "Интересует автоматизация обработки заявок.",
        "source": Lead.Source.TELEGRAM_BOT,
        "status": Lead.Status.CLOSED,
        "tags": ("automation",),
    },
    {
        "name": "Демо: Глеб Орлов",
        "contact": "gleb.demo@example.com",
        "request": "Нужен аудит учебного лендинга и рекламных материалов.",
        "source": Lead.Source.MANUAL,
        "status": Lead.Status.NEW,
        "tags": ("website", "advertising"),
    },
    {
        "name": "Демо: Дарья Волкова",
        "contact": "darya.demo@example.com",
        "request": "Проверить интеграцию формы обратной связи.",
        "source": Lead.Source.TELEGRAM_BOT,
        "status": Lead.Status.IN_PROGRESS,
        "tags": ("other",),
    },
    {
        "name": "Демо: Егор Фомин",
        "contact": "egor.demo@example.com",
        "request": "Вопрос по возможностям агентства.",
        "source": Lead.Source.MANUAL,
        "status": Lead.Status.NEW,
        "tags": (),
    },
]


class Command(BaseCommand):
    help = "Create a one-time set of fictional CRM leads for product review."

    def handle(self, *args, **options):
        CRMState.objects.get_or_create(pk=1)
        with transaction.atomic():
            state = CRMState.objects.select_for_update().get(pk=1)
            if state.demo_seeded_at is not None:
                self.stdout.write("Demo leads have already been seeded.")
                return

            tags = {
                tag.code: tag
                for tag in Tag.objects.filter(
                    code__in={code for row in DEMO_LEADS for code in row["tags"]}
                )
            }
            missing = {code for row in DEMO_LEADS for code in row["tags"]} - tags.keys()
            if missing:
                raise CommandError("Apply CRM migrations before seeding demo leads.")

            next_sequence = state.last_arrival_sequence
            for row in DEMO_LEADS:
                contacts = validate_contacts([row["contact"]])
                next_sequence += 1
                lead = Lead.objects.create(
                    name=row["name"],
                    request=row["request"],
                    source=row["source"],
                    status=row["status"],
                    is_demo=True,
                    arrival_sequence=next_sequence,
                )
                LeadContact.objects.bulk_create(
                    [
                        LeadContact(
                            lead=lead,
                            type=contact.type,
                            value=contact.value,
                            key=contact.key,
                            position=index,
                        )
                        for index, contact in enumerate(contacts)
                    ]
                )
                lead.tags.set([tags[code] for code in row["tags"]])
            state.last_arrival_sequence = next_sequence
            state.demo_seeded_at = timezone.now()
            state.save(update_fields=["last_arrival_sequence", "demo_seeded_at"])
        self.stdout.write(self.style.SUCCESS(f"Created {len(DEMO_LEADS)} fictional demo leads."))
