# CFE MCP

Serveur MCP local pour utiliser son espace adhérent **CFE** (Caisse des Français de l'Étranger)
depuis Claude : remboursements, décomptes PDF, démarches, messagerie, demandes de remboursement,
réexamens et changement du compte bancaire de versement.

Projet personnel, non officiel, sans lien avec la CFE. Tout tourne sur votre machine.

## Installation dans Claude Desktop

Prérequis : macOS ou Linux, [uv](https://docs.astral.sh/uv/getting-started/installation/),
Google Chrome.

1. Claude → Réglages → Développeur → **Modifier la configuration**
   (`claude_desktop_config.json`).
2. Ajouter :

~~~json
{
  "mcpServers": {
    "cfe": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/eliemada/cfe-mcp-server", "cfe-mcp"]
    }
  }
}
~~~

3. Redémarrer Claude, puis demander : « Connecte-moi à la CFE ».
   Chrome s'ouvre : saisir identifiants et code e-mail. Le mot de passe ne passe jamais par Claude.

Si Claude ne trouve pas `uvx`, mettre son chemin complet (résultat de `which uvx`).
Dépôt privé : il faut un accès GitHub configuré dans git (`gh auth login`).

Claude Code : `claude mcp add cfe -- uvx --from git+https://github.com/eliemada/cfe-mcp-server cfe-mcp`

## Ce qui fonctionne

Deux connexions distinctes, chacune avec ses outils :

| Connexion | Fonctions | Disponibilité |
| --- | --- | --- |
| Portail web (`cfe_start_login`) | Démarches, messages reçus, compte bancaire des remboursements | Dès l'installation |
| API mobile (`cfe_mobile_start_login`) | Bénéficiaires, remboursements, décomptes PDF, catalogue des soins, demandes, réexamens | Nécessite en plus un fichier de configuration technique **non fourni** dans ce dépôt |

Sans ce fichier (`~/.local/share/cfe-mcp/mobile-service-config.json`), les outils de l'API mobile
répondent par une erreur explicite ; ceux du portail fonctionnent normalement.

## Exemples

- « Liste mes démarches CFE en cours. »
- « Montre mes derniers messages de la CFE. »
- « Liste mes remboursements et télécharge le dernier décompte. »
- « Prépare une demande de remboursement avec cette facture, sans l'envoyer. »

## À savoir

- **Les envois sont réels.** `cfe_submit_claim`, `cfe_submit_review` et `cfe_submit_bank_change`
  déposent réellement à la CFE. `cfe_upload_claim_attachments` et
  `cfe_validate_claim(server_check=true)` lui transmettent déjà pièces et soins.
  Le reste est lecture ou brouillon local.
- Le dépôt de nouveaux soins par l'API mobile n'a **pas encore été vérifié en production**
  (tests simulés uniquement). Réexamen et changement bancaire ont été vérifiés en réel.
- Après une coupure, l'état passe à `uncertain` : vérifier sur le site CFE, ne pas renvoyer.
- Un accusé confirme la réception, pas le remboursement.
- Pièces jointes : PDF, JPEG ou PNG, 3 Mio maximum, métadonnées retirées avant envoi.
- Les données restent dans `~/.local/share/cfe-mcp` (accès privé, **non chiffré**).
  `CFE_DATA_DIR` change l'emplacement.
- La session du portail expire : relancer la connexion quand Claude le demande.

## Développement

~~~sh
git clone https://github.com/eliemada/cfe-mcp-server && cd cfe-mcp-server
uv sync --locked
uv run pytest -q          # 180 tests, aucune écriture CFE réelle
uv run ruff check src tests
uv run cfe --help         # ligne de commande équivalente
~~~

Documentation : [demandes](docs/CLAIMS.md) · [réexamens](docs/REVIEWS.md) ·
[banque](docs/BANKING.md) · [métadonnées](docs/METADATA.md) ·
[API mobile](docs/MOBILE_API.md) · [portail](docs/API.md) · [vérification](docs/VERIFICATION.md)
