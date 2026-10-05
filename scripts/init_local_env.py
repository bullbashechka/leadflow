"""Create local secrets without printing them or replacing an existing .env."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
example = (root / ".env.example").read_text()
content = example.replace("DJANGO_SECRET_KEY=\n", f"DJANGO_SECRET_KEY={secrets.token_hex(32)}\n")
content = content.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={secrets.token_hex(24)}\n")
try:
    with (root / ".env").open("x") as output:
        output.write(content)
    (root / ".env").chmod(0o600)
except FileExistsError:
    print(".env already exists; left unchanged.")
else:
    print("Created .env with local credentials. Add BOT_TOKEN only to this file.")
