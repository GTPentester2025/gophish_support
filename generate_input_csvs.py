"""
Generate Gophish-format CSV userlists in prod/input/.

Format matches the official template:
  First Name, Last Name, Email, Position
"""

import csv
import os
import random

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(SCRIPT_DIR, "input")

# 22 files, 30–100 users each, with French/Spanish accented names and emails.
FILE_SPECS = [
    ("userbase_01_fr_sales.csv", 34, "fr"),
    ("userbase_02_es_marketing.csv", 41, "es"),
    ("userbase_03_fr_hr.csv", 52, "fr"),
    ("userbase_04_es_finance.csv", 38, "es"),
    ("userbase_05_mixed_ops.csv", 67, "mixed"),
    ("userbase_06_fr_legal.csv", 45, "fr"),
    ("userbase_07_es_it.csv", 73, "es"),
    ("userbase_08_fr_support.csv", 30, "fr"),
    ("userbase_09_es_logistics.csv", 88, "es"),
    ("userbase_10_mixed_exec.csv", 55, "mixed"),
    ("userbase_11_fr_research.csv", 62, "fr"),
    ("userbase_12_es_procurement.csv", 47, "es"),
    ("userbase_13_fr_training.csv", 91, "fr"),
    ("userbase_14_es_customer.csv", 36, "es"),
    ("userbase_15_mixed_engineering.csv", 79, "mixed"),
    ("userbase_16_fr_quality.csv", 44, "fr"),
    ("userbase_17_es_compliance.csv", 58, "es"),
    ("userbase_18_fr_product.csv", 33, "fr"),
    ("userbase_19_es_analytics.csv", 96, "es"),
    ("userbase_20_mixed_allhands.csv", 100, "mixed"),
    ("userbase_21_fr_bilingual.csv", 42, "fr"),
    ("userbase_22_es_accents.csv", 51, "es"),
]

FR_FIRST = [
    "François", "Élise", "André", "Renée", "Zoé", "Chloé", "Léa", "Noémie",
    "Gaël", "Maël", "Jérôme", "Hélène", "Céline", "Benoît", "Anaïs", "Océane",
]
FR_LAST = [
    "Dupont", "Lefèvre", "Moreau", "García", "Müller", "Façon", "Çelik",
    "Björk", "O'Connor", "d'Artagnan", "Saint-Étienne", "Beaulieu",
]
ES_FIRST = [
    "José", "María", "Ángel", "Iñaki", "Soledad", "Peña", "Niño", "Luís",
    "Ramón", "Concepción", "Belén", "Ñoño", "Ximena", "Rocío",
]
ES_LAST = [
    "García", "Martínez", "Hernández", "López", "González", "Rodríguez",
    "Fernández", "Pérez", "Sánchez", "Ramírez", "Muñoz", "Peña", "Cañón",
]
EN_FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley"]
EN_LAST = ["Smith", "Johnson", "Brown", "Davis", "Wilson"]
POSITIONS_FR = [
    "Responsable des ventes", "Ingénieur systèmes", "Analyste sécurité",
    "Chef de projet", "Technicien support",
]
POSITIONS_ES = [
    "Gerente de ventas", "Ingeniero de sistemas", "Analista de seguridad",
    "Jefe de proyecto", "Técnico de soporte",
]
POSITIONS_EN = [
    "Sales Manager", "Systems Engineer", "Security Analyst",
    "Project Lead", "Support Technician",
]
DOMAINS = ["example.com", "demo.org", "test.local", "échantillon.fr", "prueba.es"]


def pick_names(locale: str, idx: int):
    if locale == "fr":
        fn = FR_FIRST[idx % len(FR_FIRST)]
        ln = FR_LAST[(idx * 3) % len(FR_LAST)]
        pos = POSITIONS_FR[idx % len(POSITIONS_FR)]
    elif locale == "es":
        fn = ES_FIRST[idx % len(ES_FIRST)]
        ln = ES_LAST[(idx * 5) % len(ES_LAST)]
        pos = POSITIONS_ES[idx % len(POSITIONS_ES)]
    else:
        if idx % 2 == 0:
            fn = FR_FIRST[idx % len(FR_FIRST)]
            ln = ES_LAST[idx % len(ES_LAST)]
            pos = POSITIONS_FR[idx % len(POSITIONS_FR)]
        else:
            fn = ES_FIRST[idx % len(ES_FIRST)]
            ln = FR_LAST[idx % len(FR_LAST)]
            pos = POSITIONS_EN[idx % len(POSITIONS_EN)]
    return fn, ln, pos


def make_email(fn: str, ln: str, idx: int, locale: str) -> str:
    """Build emails that stress encoding (accents in local-part for some rows)."""
    base_local = (
        f"{fn.lower().replace(' ', '')}.{ln.lower().replace(' ', '').replace(chr(39), '')}"
    )
    base_local = (
        base_local.replace("é", "e")
        .replace("è", "e")
        .replace("ê", "e")
        .replace("ë", "e")
        .replace("á", "a")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
        .replace("ü", "u")
        .replace("ö", "o")
        .replace("ç", "c")
        .replace("ï", "i")
        .replace("ö", "o")
    )
    domain = DOMAINS[idx % len(DOMAINS)]
    # ~15% keep accented local-part to detect upload corruption
    if idx % 7 == 0 and locale in ("fr", "mixed"):
        local = f"{fn.split()[0].lower()}.{ln.split()[0].lower()}{idx}"
        local = local.replace(" ", "")
    elif idx % 9 == 0 and locale in ("es", "mixed"):
        local = f"{fn[:3].lower()}{ln[:4].lower()}{idx}"
    else:
        local = f"{base_local}{idx}"
    return f"{local}@{domain}"


def generate_rows(count: int, locale: str) -> list:
    rows = []
    for i in range(count):
        fn, ln, pos = pick_names(locale, i)
        email = make_email(fn, ln, i, locale)
        rows.append(
            {
                "First Name": fn,
                "Last Name": ln,
                "Email": email,
                "Position": pos,
            }
        )
    return rows


def write_csv(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["First Name", "Last Name", "Email", "Position"],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    os.makedirs(INPUT_DIR, exist_ok=True)
    total_users = 0
    for filename, count, locale in FILE_SPECS:
        path = os.path.join(INPUT_DIR, filename)
        rows = generate_rows(count, locale)
        write_csv(path, rows)
        total_users += count
        print(f"  {filename}: {count} users")
    print(f"\nCreated {len(FILE_SPECS)} CSV files ({total_users} users total) in {INPUT_DIR}")


if __name__ == "__main__":
    main()
