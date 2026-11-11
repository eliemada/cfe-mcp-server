"""Local CLI for authentication and inspection of the verified API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .auth import BrowserLogin, import_browser
from .claims import DOCUMENT_TYPES, ClaimManager
from .client import AuthenticationRequired, CfeClient, CfeError
from .mobile import MobileSession
from .mobile_api import MobileApiClient
from .mobile_browser import MobileBrowserLogin
from .research import catalog_har
from .reviews import ReviewManager
from .storage import read_private_json


def catalog_file(path: str) -> object:
    return catalog_har(read_private_json(Path(path).expanduser()))


def claim_file(path: str) -> object:
    return read_private_json(Path(path).expanduser())


async def run(args: argparse.Namespace) -> object:
    if args.command in {"login", "mobile-login"}:
        if args.command == "login" and args.cdp:
            return await import_browser(args.cdp)
        browser = MobileBrowserLogin() if args.command == "mobile-login" else BrowserLogin()
        try:
            result = await browser.start()
            if result.get("authenticated"):
                return result
            print(result["message"], file=sys.stderr)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                try:
                    return await browser.finish()
                except AuthenticationRequired:
                    await asyncio.sleep(2)
            raise CfeError(f"Délai de connexion dépassé. Relancer `cfe {args.command}`.")
        finally:
            await browser.close()
    if args.command == "catalog":
        return await asyncio.to_thread(catalog_file, args.har)
    if args.command == "mobile-status":
        async with MobileSession() as session:
            return await session.status(force_refresh=args.refresh)
    if args.command in {
        "review-prepare", "review-import", "reviews", "review", "review-update",
        "review-attach", "review-remove-attachment", "review-validate", "review-export",
        "review-submit",
    }:
        async with MobileApiClient() as client:
            reviews = ReviewManager(client)
            if args.command in {"review-prepare", "review-update", "review-import"}:
                values = await asyncio.to_thread(claim_file, args.json_file)
                if args.command == "review-prepare":
                    return await reviews.prepare(values)
                if args.command == "review-import":
                    return await reviews.import_claim(args.draft_id, args.revision, values)
                return await reviews.update(args.draft_id, args.revision, values)
            if args.command == "reviews":
                return await reviews.list()
            if args.command == "review":
                return await reviews.get(args.draft_id, args.refresh)
            if args.command == "review-submit":
                return await reviews.submit(args.draft_id, args.revision)
            if args.command == "review-attach":
                return await reviews.attach(args.draft_id, args.path, args.type)
            if args.command == "review-remove-attachment":
                return await reviews.remove(args.draft_id, args.attachment_id)
            if args.command == "review-validate":
                return await reviews.validate(args.draft_id)
            return await reviews.export(args.draft_id, args.revision)
    if args.command in {
        "care-schema",
        "prepare",
        "claim",
        "claims",
        "update",
        "attach",
        "validate",
        "upload",
        "remove-attachment",
        "submit",
    }:
        async with MobileApiClient() as client:
            manager = ClaimManager(client)
            if args.command == "care-schema":
                return await manager.schema(args.code)
            if args.command in {"prepare", "update"}:
                claim = await asyncio.to_thread(claim_file, args.json_file)
                if args.command == "prepare":
                    return await manager.prepare(claim)
                return await manager.update(args.draft_id, args.revision, claim)
            if args.command == "claims":
                return await manager.list()
            if args.command == "claim":
                return await manager.get(args.draft_id)
            if args.command == "attach":
                return await manager.attach(args.draft_id, args.path, args.type)
            if args.command == "validate":
                return await manager.validate(args.draft_id, args.server)
            if args.command == "upload":
                return await manager.upload(args.draft_id)
            if args.command == "remove-attachment":
                return await manager.remove(args.draft_id, args.attachment_id)
            return await manager.submit(args.draft_id, args.revision)
    if args.command not in {"requests", "portal-status", "messages", "messaging-options"}:
        async with MobileApiClient() as client:
            if args.command == "status":
                return await client.session_status()
            if args.command == "options":
                return await client.claim_options(args.kind, args.country)
            if args.command == "beneficiaries":
                return await client.beneficiaries()
            if args.command == "care-types":
                return await client.care_types(args.search)
            if args.command == "download":
                return await client.download_statement(args.reimbursement_id)
            return await client.reimbursements(args.page, args.page_size)
    async with CfeClient() as client:
        if args.command == "portal-status":
            return await client.session_status()
        if args.command == "messaging-options":
            return await client.messaging_options()
        return await client.grid(args.command, args.page, args.page_size)


def main() -> None:
    parser = argparse.ArgumentParser(description="CFE — compte, décomptes et demandes personnelles")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("login", help="Connexion interactive dans Chrome")
    auth.add_argument("--cdp", help="Importer un onglet CFE via un CDP local")
    commands.add_parser("mobile-login", help="Connexion OAuth mobile dans Chrome")
    mobile_status = commands.add_parser("mobile-status", help="Vérifier l'identité mobile")
    mobile_status.add_argument("--refresh", action="store_true", help="Tester un renouvellement")
    commands.add_parser("status", help="Vérifier la session")
    commands.add_parser("portal-status", help="Vérifier la session du portail pour les démarches")
    commands.add_parser("messaging-options", help="Lire les thèmes de la messagerie du portail")
    care_types = commands.add_parser("care-types", help="Chercher un type de soins")
    care_types.add_argument("--search", default="")
    schema = commands.add_parser("care-schema", help="Champs et règles d'un type de soins")
    schema.add_argument("code")
    prepare = commands.add_parser("prepare", help="Préparer un brouillon depuis un fichier JSON")
    prepare.add_argument("json_file")
    commands.add_parser("claims", help="Lister les dossiers créés avec ce MCP")
    review_prepare = commands.add_parser("review-prepare", help="Préparer un réexamen local")
    review_prepare.add_argument("json_file")
    commands.add_parser("reviews", help="Lister les réexamens locaux")
    for name, help_text in {
        "review": "Relire un réexamen local",
        "review-import": "Transférer un brouillon de soins vers un réexamen",
        "review-update": "Modifier un réexamen local",
        "review-attach": "Joindre une pièce locale à un réexamen",
        "review-remove-attachment": "Retirer une pièce locale d'un réexamen",
        "review-validate": "Contrôler localement un réexamen",
        "review-export": "Exporter le message et les pièces, sans envoi",
        "review-submit": "Envoyer le réexamen demandé, ses pièces et vérifier la réception",
    }.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("draft_id")
        if name in {"review-import", "review-update", "review-export", "review-submit"}:
            command.add_argument("--revision", type=int, required=True)
        if name == "review":
            command.add_argument("--refresh", action="store_true", help="Vérifier sans renvoyer")
        if name in {"review-import", "review-update"}:
            command.add_argument("json_file")
        if name == "review-attach":
            command.add_argument("path")
            command.add_argument("--type", choices=sorted(DOCUMENT_TYPES), required=True)
        if name == "review-remove-attachment":
            command.add_argument("attachment_id")
    for name, help_text in {
        "claim": "Relire un dossier local",
        "update": "Remplacer les données d'un brouillon avant upload",
        "attach": "Joindre un fichier local sans le transmettre",
        "validate": "Valider le dossier et ses pièces",
        "upload": "Transmettre les pièces à la CFE après contrôle des soins",
        "remove-attachment": "Retirer une pièce, aussi à la CFE si téléversée",
        "submit": "Déposer réellement le dossier à la CFE",
    }.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("draft_id")
        if name in {"update", "submit"}:
            command.add_argument("--revision", type=int, required=True)
        if name == "update":
            command.add_argument("json_file")
        if name == "attach":
            command.add_argument("path")
            command.add_argument("--type", choices=sorted(DOCUMENT_TYPES), required=True)
        if name == "validate":
            command.add_argument(
                "--server", action="store_true", help="Contrôler les soins à la CFE"
            )
        if name == "remove-attachment":
            command.add_argument("attachment_id")
    download = commands.add_parser("download", help="Télécharger un décompte PDF du compte")
    download.add_argument("reimbursement_id")
    options = commands.add_parser("options", help="Lire les options du formulaire de remboursement")
    options.add_argument("--kind", choices=["etr", "fra"], default="etr")
    options.add_argument("--country", default="")
    for name in ("reimbursements", "requests", "beneficiaries", "messages"):
        listing = commands.add_parser(name)
        listing.add_argument("--page", type=int, default=1)
        listing.add_argument("--page-size", type=int, default=10)
    catalog = commands.add_parser(
        "catalog", help="Extraire la structure des requêtes d'un HAR local"
    )
    catalog.add_argument("har")
    try:
        result = asyncio.run(run(parser.parse_args()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except CfeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, TypeError):
        print("Stockage ou réponse CFE invalide ; aucun renvoi automatique.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
