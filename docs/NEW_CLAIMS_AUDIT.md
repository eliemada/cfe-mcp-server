# Audit du dépôt mobile - 11 novembre 2026

Le dépôt de **nouvelles** demandes est implémenté dans le MCP. Son contrat a été reconstitué
depuis l'APK CFE & moi 3.7.0 et comparé au schéma d'actions publié par le serveur Dataverse.
Les lectures du compte fonctionnent en production. Les écritures du parcours mobile restent
testées avec un transport simulé : aucun nouveau soin réel n'a encore été soumis par ce MCP.

## Parcours disponible

| Étape | Outil MCP | État de preuve |
| --- | --- | --- |
| Session renouvelable et bénéficiaires | `cfe_session_status`, `cfe_list_beneficiaries` | Compte réel |
| Pays, devise, circonstance et formulaire | `cfe_claim_options`, `cfe_care_schema` | Catalogue réel et asset APK |
| Brouillon et pièces figées | `cfe_prepare_claim`, `cfe_add_attachment` | Tests et brouillon réel local |
| Contrôle des soins | `cfe_validate_claim(server_check=true)` | APK, schéma serveur et tests simulés |
| Upload des pièces nettoyées | `cfe_upload_claim_attachments` | APK, schéma serveur et tests simulés |
| Retrait d'une pièce de ce brouillon | `cfe_remove_attachment` | APK, schéma serveur et tests simulés |
| Dépôt final avec révision | `cfe_submit_claim` | APK, schéma serveur et tests simulés |
| Accusé local et prévention des renvois | `cfe_get_claim` | Coupures, concurrence et doublons testés |

Ce parcours utilise l'authentification mobile renouvelable. La session web distincte concerne
l'historique, la messagerie et les réexamens ; elle n'est pas requise pour ces écritures mobiles.
Le parcours utilisateur détaillé figure dans [CLAIMS.md](CLAIMS.md).

## Concordance avec les sources

Les 88 définitions de `data/care_forms.json` correspondent au contenu JSON de
`acte_details_code_data.json` extrait de l'application. Tous leurs champs ont une correspondance
dans `care.py`, y compris l'alias `absDatePresc` du booléen d'absence de prescription.
Les 85 codes feuilles actifs observés dans le catalogue serveur sont couverts. Les trois
définitions inactives ne sont pas proposées comme soins disponibles.

| Action mobile | Paramètres du schéma Dataverse, tous de type chaîne | Preuve ARM64 |
| --- | --- | --- |
| `new_api_applimob_checkCare` | `id`, `body`, `token` | `0x382d24`, enveloppe `incident` + `soin` |
| `new_api_applimob_addDocument` | `ref_id`, `token`, `file_content`, `file_name`, `type` | `0x313984` |
| `new_api_applimob_deleteDocument` | `token`, `file_id` | `0x379c04` |
| `new_api_applimob_sendRequest` | `id`, `careId`, `body`, `token` | `0x393270`, enveloppe `incident` + `soins` |

`body` est une chaîne contenant du JSON, pas un objet directement inséré dans la requête.
Le sérialiseur d'un soin (`0x382f54` à `0x383474`) comporte 21 clés, dont `new_verification`
pour le code feuille. Le sérialiseur d'incident (`0x383474` à `0x3836ec`) correspond aux champs
reconstitués de bénéficiaire, pays, devise, circonstance et commentaire. Les dates suivent
`yyyy-MM-dd`. La date conditionnelle de circonstance ne reçoit jamais la date du jour par défaut.

Le premier upload fournit un `ref_id` vide ; la référence retournée est conservée pour les
pièces suivantes et comme `careId` au dépôt. La taille maximale trouvée dans l'APK est
3 145 728 octets. Les limites de nombre de pièces et de soins proviennent de `getParams` et
sont relues avant envoi. Le contrôle serveur précède les uploads.

Les preuves brutes restent dans le dossier privé `~/.local/share/cfe-mcp/research/` :
`android/annotated.asm`, `android/acte_details_code_data.json` et
`messaging/api-discovery/dataverse-metadata.xml`. Les constantes d'authentification et les
jetons ne font pas partie de la distribution. La version et les empreintes APK figurent dans
[MOBILE_API.md](MOBILE_API.md).

## Ce qui manque pour une validation complète

Une dépense réelle non encore déclarée, avec les informations de soins exactes, les justificatifs
du titulaire et sa demande de transmission. Elle permettra d'observer les réponses réelles de
`checkCare`, des uploads et de `sendRequest`, puis de rapprocher l'accusé avec la démarche CFE.
Un résultat `accepted_by_api` confirme seulement l'acceptation de dépôt par cette API ; il ne
signifie pas que le remboursement est accordé.

Le journal bloque les répétitions et les nouveaux brouillons des mêmes soins connus de ce MCP.
Il ne détecte pas automatiquement tous les dépôts antérieurs effectués manuellement, dans
Doctolib ou dans l'application mobile. Il faut vérifier l'historique avant de traiter une
dépense supposée non remboursée. Une écriture mobile incertaine exige encore une vérification
dans l'application ou le portail : aucun rapprochement automatique fiable n'a été observé.

Le réexamen déjà envoyé par la messagerie concerne une dépense existante. Il ne constitue
pas une preuve de dépôt mobile et ne doit pas être redéposé comme une nouvelle demande.
