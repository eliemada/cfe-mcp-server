# Réexaminer un remboursement existant

Ce parcours prépare un message concernant une demande CFE déjà déclarée. Il conserve la
référence de cette demande, le texte fourni et les pièces dans un brouillon privé local.
Les outils de préparation ne créent pas de nouveau soin et ne transmettent rien.
`cfe_submit_review` réalise ensuite l'envoi demandé par le titulaire via le portail, puis
vérifie le statut, les pièces reçues et le texte du récapitulatif officiel.

## Fonctions disponibles

| Besoin | Outil MCP |
| --- | --- |
| Lire les thèmes actuels du portail | `cfe_messaging_options` |
| Consulter les messages reçus | `cfe_list_messages` |
| Préparer un réexamen local | `cfe_prepare_review` |
| Transférer un brouillon de soins et ses pièces | `cfe_import_claim_as_review` |
| Relire ou lister les réexamens | `cfe_get_review`, `cfe_list_reviews` |
| Modifier le texte avec la révision courante | `cfe_update_review` |
| Ajouter ou retirer une pièce locale | `cfe_add_review_attachment`, `cfe_remove_review_attachment` |
| Vérifier les données et les empreintes | `cfe_validate_review` |
| Exporter le message et les pièces | `cfe_export_review` |
| Envoyer le réexamen avec sa révision courante | `cfe_submit_review` |
| Vérifier un envoi existant sans le répéter | `cfe_get_review(refresh=true)` |

Les outils de brouillon utilisent l'identité mobile pour isoler les comptes. Les lectures,
l'envoi et la vérification distante du portail nécessitent sa session distincte :
`uv run cfe login` ouvre Chrome
pour les identifiants et le code e-mail. Les champs de connexion ne sont pas enregistrés dans
le dossier de réexamen.

## Données du brouillon

`beneficiary_id` doit appartenir au compte connecté. `request_number` est la référence de la
demande CFE existante, et non l'identifiant local du MCP. `statement_reference` est facultatif.
`subject` et `message` reprennent les faits fournis. `care_location` vaut `etr` ou `fra`.

Le contrôle local valide les champs et les fichiers. Avant l'envoi, le MCP vérifie aussi que
la référence existe dans l'historique du compte et que les sessions mobile et portail ont la
même identité. Ces contrôles ne déterminent ni l'exactitude médicale du texte ni l'éligibilité
au remboursement. Les informations de soins ne sont jamais déduites du montant ou d'une pièce.

Les pièces sont des copies figées, avec empreinte SHA-256 : PDF, JPEG ou PNG, au maximum 3 Mio
à l'import local et 10 pièces par réexamen. La validation et l'envoi limitent chaque copie
nettoyée à 3 000 000 octets. Le nombre maximal de pièces accepté par la messagerie n'est pas
vérifié ; le parcours réel a reçu trois pièces. L'objet préfixé par « Objet : », les sauts de
ligne et le message doivent tenir ensemble dans les 2 500 unités UTF-16 du champ du portail.
Le retrait enlève la pièce du brouillon et des futurs exports ; sa copie privée est conservée.

## Transférer un brouillon existant

1. Lire `cfe_get_claim` pour obtenir sa révision courante.
2. Appeler `cfe_import_claim_as_review` avec cette révision, la référence CFE et le message.
3. Relire le réexamen, contrôler ses pièces, puis l'exporter ou l'envoyer avec sa propre révision.

L'import est réservé à un dossier en état `draft`, sans opération d'écriture enregistrée ni
pièce téléversée. Il conserve les pièces et bloque l'ancien parcours de dépôt avant de copier
les fichiers. Les outils de modification, contrôle serveur, upload et dépôt de l'ancien
brouillon refusent ensuite l'opération et renvoient vers les outils de réexamen.

En cas d'interruption pendant l'import, répéter le même appel avec la même révision source.
Le blocage de l'ancien dépôt reste actif et l'import reprend sous le même identifiant de
réexamen. Un import déjà terminé renvoie le même brouillon si ses champs n'ont pas changé.

## Export local

L'export crée `message.txt` et des copies nettoyées des métadonnées dans :

```text
~/.local/share/cfe-mcp/reviews/<draft_id>/export-<revision>-metadata-v1/
```

Le dossier est en 0700 et ses fichiers en 0600. Les empreintes sont vérifiées au moment de
l'export. Une nouvelle révision produit un autre dossier. Répéter l'export à l'identique est
possible, mais un fichier exporté puis modifié par l'utilisateur n'est pas écrasé.
Les originaux restent inchangés. Les champs `source_sha256` et `sha256` distinguent la pièce
d'origine de la copie exportée. Les anciens exports bruts restent conservés, mais les nouveaux
exports utilisent obligatoirement la [politique de suppression des métadonnées](METADATA.md).

Commandes correspondantes :

```sh
uv run cfe messaging-options
uv run cfe messages --page 1 --page-size 10
uv run cfe reviews
uv run cfe review-prepare --help
uv run cfe review-import --help
uv run cfe review-export --help
uv run cfe review-submit --help
uv run cfe review <draft_id> --refresh
```

## Envoi et vérification

Après la demande explicite de transmission du titulaire, appeler
`cfe_submit_review(draft_id, expected_revision)`. Le MCP nettoie et vérifie les pièces,
rattache le brouillon au compte du portail, contrôle la demande d'origine, crée le message,
joint les copies nettoyées et utilise le bouton `Envoyer` du formulaire observé.

Un accusé affiché seul ne suffit pas. La réussite nécessite la navigation de confirmation,
le statut du même incident dans l'historique signé, les empreintes des pièces relues sur le
serveur et le texte intégral approuvé dans le PDF récapitulatif créé par la CFE. Le résultat
contient `remote_id`, `receipt`, `last_stage`, le chemin privé du PDF et son empreinte.
`delivery.remote_submission_supported` vaut `true` ; `delivery.submitted` reflète l'état local.

Le verrou entre processus couvre la soumission. L'intention est écrite sur disque avant
chaque action susceptible de créer ou transmettre des données. Une coupure après cette
intention place le dossier en `uncertain`. Aucune action d'écriture n'est alors répétée.
`cfe_get_review(refresh=true)` peut vérifier le dossier distant déjà connu et confirmer
l'envoi ; il ne crée rien et ne reprend pas les étapes manquantes. Si l'identifiant distant
n'a pas été reçu, il faut retrouver la démarche dans le portail.

Un second appel sur un dossier `submitted` renvoie l'accusé enregistré. Le journal refuse
aussi un nouveau brouillon pour la même demande et le même décompte, même si son texte ou ses
pièces changent. Les anciens envois supervisés sont pris en compte. Les états `sending`,
`uncertain` et `submitted` bloquent toute modification du texte ou des pièces. Pour une relance,
utiliser la conversation existante ; la création automatique de messages de suivi n'est pas
implémentée. Un export seul n'est jamais une preuve d'envoi.

## Preuve réelle et portée des tests

Le 11 novembre 2026, la première étape authentifiée de la messagerie a été lue sans exécuter
son JavaScript. Elle propose « Signaler une anomalie sur un remboursement perçu : Remboursement
de soins », associé au formulaire « Ecrire un message - Remboursement Soin ».

Après autorisation explicite du titulaire, un réexamen réel a été envoyé le même jour par le
parcours du portail. Le premier `Suivant` crée un incident. La seconde étape contient
`description` (2 500 caractères) et `#Subgrid_PJ`. Les trois copies nettoyées ont été jointes
dans des fenêtres d'ajout liées à cet incident, puis le bouton visible `Envoyer` a déclenché
le postback final. La CFE a affiché « Votre message a bien été pris en compte. » et la démarche
correspondante a été retrouvée avec le statut « Envoyée à la CFE ».

Cet envoi a utilisé un script supervisé privé, avec vérification du compte, des empreintes
autorisées et un journal avant chaque transition. Le parcours observé est maintenant intégré
dans `review_delivery.py`, `reviews.py`, le MCP et la CLI. Les tests de cette intégration
exercent un vrai navigateur dont toutes les requêtes sont interceptées, les erreurs de compte
et de formulaire, trois uploads, l'accusé, la relecture des documents et les coupures du journal.
Ils ne constituent pas un second envoi de production avec le nouvel outil. Le dossier réel
déjà envoyé et son brouillon de soins source restent bloqués contre un doublon.
