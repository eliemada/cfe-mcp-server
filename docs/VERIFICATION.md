# Vérification du MCP - 11 novembre 2026

## Résultat

Le serveur MCP est installé dans Codex et ses lectures fonctionnent avec le compte du titulaire.
Un dossier réel et ses trois pièces ont été transférés vers un réexamen local puis exportés.
Après autorisation explicite du titulaire, ce réexamen a été envoyé par le portail et son
statut « Envoyée à la CFE » vérifié. Son ancien parcours de nouveau dépôt reste bloqué.
Les écritures de nouveaux soins par l'API mobile restent à vérifier en production.

## Preuves en ligne

| Fonction | Vérification |
| --- | --- |
| OAuth B2C natif + PKCE | Connexion et vérification cryptographique réussies |
| Renouvellement OAuth | HTTP 200 sans nouveau code e-mail, même identité |
| Authentification technique puis compte | Accès métier accordé pour le titulaire |
| Bénéficiaires | 1 bénéficiaire observé ; identifiant titulaire contrôlé |
| Remboursements | 6 décomptes observés |
| Catalogue | 85 codes feuilles, tous couverts par les schémas embarqués |
| PDF | Décompte du compte téléchargé, signature PDF et empreinte vérifiées |
| Protocole MCP stdio | 40 outils dans la version actuelle ; lectures du compte, messagerie et réexamens interrogées avec succès |
| Brouillon réel | Création MCP, copie de trois pièces, contrôle local réussi ; état `draft`, pièces `local` |
| Réexamen réel | Envoi autorisé par le portail, même incident retrouvé au statut « Envoyée à la CFE », accusé relisible par MCP ; ancien dépôt bloqué |
| Justificatifs du réexamen | Trois copies nettoyées jointes, puis relues par l'API du portail avec des empreintes identiques |
| Récapitulatif officiel | PDF CFE téléchargé, référence et texte autorisé vérifiés |
| Messagerie | Session renouvelée ; 38 messages reçus et 7 thèmes lus via MCP ; parcours complet d'envoi supervisé vérifié |
| Installation Codex | Serveur `cfe` activé, lancement uv local en stdio |

Aucune action sur un autre compte, aucun upload ni contrôle de soins inventés. Les requêtes
de contrôle serveur et de dépôt de nouveaux soins restent absentes des essais réels ; seul
`server_check=false` a été exécuté sur l'ancien brouillon de soins. La découverte supplémentaire
a lu le schéma Dataverse et testé des lectures ciblées du dossier connu via le portail.

## Vérifications automatisées

`uv run pytest -q` : **180 tests réussis**. `uv run ruff check src tests` : **succès**.

- Signatures, destinataire, émetteur, nonce, état, PKCE, expiration et révocation OAuth.
- Renouvellements concurrents et séparation des identités.
- Refus des URL techniques non autorisées et des redirections transportant des secrets.
- Appartenance des bénéficiaires et décomptes au compte connecté.
- Sérialisation des demandes, champs obligatoires, dates, montants et prescriptions.
- Format et limite de pièce, doublons et modifications après préparation.
- Contrôle serveur refusé : aucun fichier téléversé.
- Référence commune des pièces et demande finale ; retrait limité aux pièces enregistrées.
- Réponse ambiguë, coupure réseau, redémarrage et journal incomplet : aucun renvoi automatique.
- Soumissions concurrentes et nouveau brouillon des mêmes soins : second dépôt bloqué.
- Dossier déjà accepté : accusé local retourné, sans nouvelle soumission.
- Processus MCP réel avec une session temporaire vide.
- Réexamen : isolement entre comptes, validation des champs et empreintes, export sans écrasement.
- Réexamen envoyé, incertain ou en cours : accusé lisible, modification du texte et des pièces refusée.
- Import interrompu et reprise : l'ancien dépôt reste bloqué, aucune écriture CFE déclenchée.
- Thèmes du portail : association lue dans le script, erreur si elle change, aucun postback.
- Grille de messages : configuration signée inchangée, cookie de pagination et CSRF renouvelé.
- Métadonnées : PDF Info/XMP, métadonnées des pages et images intégrées, JPEG progressifs,
  PNG transparents, orientation, signatures protégées et refus avant toute transmission.
- Les octets réellement remis aux adaptateurs d'upload simulés sont les copies nettoyées.
- Réexamen : journal global avant écriture, récupération après coupure et double appel sans renvoi.
- Création d'un autre brouillon du même réexamen : refus, y compris après un ancien envoi supervisé.
- Navigateur réel, réseau entièrement intercepté : compte, formulaire, incident des pièces,
  trois uploads, intégrité du message et confirmation du serveur.
- Relecture : statut seul insuffisant ; fichiers exacts et texte intégral du PDF requis.

Les trois pièces réelles du réexamen ont été nettoyées via le MCP puis contrôlées avec ExifTool.
Le rendu de chaque page à 144 dpi et le texte extrait sont identiques aux originaux. Les
empreintes des originaux restent conservées séparément des empreintes des copies d'envoi.

Les scénarios de dépôt mobile sont simulés avec `httpx.MockTransport`. Ils vérifient le code
local, pas la compatibilité effective de ces écritures avec le serveur de production. L'envoi
de réexamen décrit ci-dessus constitue une vérification distincte du parcours réel du portail.
Le nouvel adaptateur général `cfe_submit_review` reprend ce parcours, mais n'a pas créé un
autre dossier réel pour son test. L'audit du contrat mobile est dans [NEW_CLAIMS_AUDIT.md](NEW_CLAIMS_AUDIT.md).

## Distribution

Wheel et archive source construits avec `uv build`. Import du wheel dans un environnement
isolé : 88 définitions de formulaire et 40 outils. Les secrets actuels de configuration
et de session sont recherchés dans le source et les archives sans être affichés ; aucune
correspondance trouvée. Les captures, APK et données de compte restent dans le dossier privé.

## Limites connues et étape restante

- L'historique des démarches et la messagerie dépendent d'une session du portail distincte.
- La durée du refresh token est une annonce serveur, pas une garantie de connexion permanente.
- Les contrôles locaux ne déterminent ni recevabilité médicale ni montant remboursable.
- Le journal ne couvre pas les dépôts manuels antérieurs. Les écritures mobiles incertaines
  ne sont pas rapprochées automatiquement avec les démarches de la CFE. Les réexamens dont
  l'incident distant est connu peuvent être vérifiés avec `cfe_get_review(refresh=true)`.
- La configuration technique privée est présente sur cette machine mais n'est pas distribuée.
- Le réexamen réel a été transmis par un script supervisé privé. L'outil MCP général reprend
  ce parcours et dispose de tests hors ligne ; il n'a pas encore créé un autre réexamen réel.

Pour achever la validation : disposer d'un dossier adapté au parcours de nouveau dépôt et d'une
instruction de transmission, contrôler puis déposer la demande voulue et confronter l'accusé
au résultat visible dans la CFE. Le dossier actuel est déjà envoyé comme réexamen ; ne pas le
réutiliser pour un nouveau dépôt.
