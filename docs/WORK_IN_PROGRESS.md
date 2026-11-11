# État de l'intégration mobile

Objectif du projet : terminer le MCP personnel de bout en bout. Le titulaire a autorisé puis
confirmé l'envoi du réexamen préparé. Ce message et ses trois pièces nettoyées ont été transmis
par le portail et leur réception a été vérifiée. Aucun nouveau soin n'a été déposé. Aucun soin
ni justificatif fictif ne doit être envoyé à la CFE.

## Vérifié en ligne le 11 novembre 2026

- OAuth B2C : connexion et renouvellement sans nouveau code e-mail.
- Authentification technique de l'application, puis `new_api_applimob_auth` : accès accordé,
  jeton adhérent et `peridt` reçus. Configuration et jetons uniquement dans le dossier privé.
- `getRecipients`, `getRemboursement`, `getParams`, `getActeDetails`, `getCurrencies`,
  `getCircumstances`, `getDocRemboursement` : succès avec le compte du titulaire.
- Un décompte PDF a été téléchargé dans `~/.local/share/cfe-mcp/downloads/`.
- 6 remboursements, 1 bénéficiaire, 85 codes de soins actifs, tous couverts par les 88 schémas
  de formulaires embarqués. Les codes locaux B_1_1, J_2 et P_1 ne sont pas des feuilles actives.
- Les lectures principales du MCP utilisent maintenant `mobile_api.py`. Le suivi historique
  des démarches et les messages reçus utilisent le portail ; sa session a été renouvelée.
- 180 tests passent. Dernière vérification Ruff : succès.
- 40 outils disponibles via le protocole MCP ; lectures du compte, catalogue et liste des brouillons
  répondent correctement. Un brouillon réel a été créé, trois pièces ont été copiées localement
  et `cfe_validate_claim(server_check=false)` a réussi. État `draft`, pièces `local`, aucun accusé.
- Ce brouillon a ensuite été transféré via MCP vers un réexamen local, contrôlé et exporté.
  Les empreintes des trois originaux sont conservées. L'ancien brouillon refuse tout nouveau dépôt.
- Nettoyage obligatoire des métadonnées avant les uploads de soins, les uploads bancaires et
  les exports et envois de réexamen. Les trois copies du dossier réel ont passé les contrôles ExifTool,
  de rendu à 144 dpi et d'extraction de texte. Après l'envoi, les trois fichiers relus sur le
  portail avaient les mêmes empreintes que les copies nettoyées autorisées.
- `cfe_list_messages` : 38 messages reçus lus ; `cfe_messaging_options` : 7 thèmes et association
  du thème de réexamen au formulaire observé. Le parcours complet de messagerie a ensuite
  été suivi avec un journal privé : création, trois pièces, message, accusé et historique.
- Réexamen : statut serveur « Envoyée à la CFE » confirmé pour le même incident ; récapitulatif
  PDF officiel téléchargé et texte autorisé retrouvé. Le dossier local est `submitted`, son
  accusé est lisible par MCP et toute modification ultérieure du texte ou des pièces est bloquée.
- Serveur `cfe` enregistré et activé dans la configuration globale locale de Codex.
- Wheel et archive source construits ; 88 schémas embarqués et 40 outils dans la version actuelle.
- Vérification de l'absence des secrets de session/configuration dans le source et les archives.

## Contrats d'écriture reconstitués, pas encore exécutés

- `new_typecontact` vaut `adh` pour le titulaire, `benef` pour un bénéficiaire lié. Getter ARM64
  `0x384010`, constantes `pp+0x17530` et `pp+0x17538`.
- Les dates sont formatées `yyyy-MM-dd` par `0x380c34`.
- `new_verification` est le code du soin le plus précis, avec suppression des espaces autour
  (`0x383990` appelle la fonction de trim `0x1d2124`). Ce n'est pas un booléen.
- `new_absencedatedescription` reprend `isPrescribed`, dont le libellé réel est « absence de
  prescription ». Le nom de variable est trompeur : vrai signifie que l'absence est cochée.
- `new_datededemandederemboursement` reçoit la date conditionnelle du formulaire de circonstance ;
  ne pas remplir automatiquement ce champ avec la date du jour. Source `0x5cec78`–`0x5cf000`.
- `checkCare` : `id` de l'adhérent, `body` JSON sérialisé avec `incident` et un seul `soin`,
  plus `token`. Le parseur traite `errorType`/`errorText`. Ne pas l'appeler avec des soins inventés.
- `addDocument` : `type`, `file_name`, `file_content` base64, `ref_id`, plus `token`.
  Types : feuille `100000000`, ordonnance `100000001`, facture `100000002`, autre `100000003`.
  Premier `ref_id` vide ; réutiliser celui reçu dans chaque fichier suivant. Réponse : `ref_id`
  et `file_id`. Source `0x313984`, `0x313b24`, `0x313c98`, `0x5c0c48`.
- Limite de fichier : **3 145 728 octets**, comparaison `0x300 << 12` à `0x314fa8`, avec message
  « plus de 3 Mo ». Les nombres de pièces et de soins viennent de `getParams` : étranger 10/5,
  France 5/1, observés aujourd'hui.
- `sendRequest` : `id` de l'adhérent, `careId` = référence commune renvoyée lors des uploads,
  `body` JSON sérialisé avec `incident` et `soins`, plus `token`. Sources `0x393270` et
  `0x5cec30` : lecture du même état `field_3b` que l'upload à `0x5c0e34`.
- `deleteDocument` : `file_id`, plus `token`. Limiter l'outil aux fichiers dont le MCP a enregistré
  l'upload pour ce compte et à une demande non soumise.

## Implémentation terminée, test réel restant

- `care.py` : schémas de soins, contraintes locales, sérialisation des champs reconstitués.
- `claims.py` : brouillons privés liés au compte, fichiers figés, validation, upload, retrait,
  soumission et accusé ; journal durable, verrou entre processus et protection contre les renvois.
- Tests : erreurs métier, coupure, redémarrage, crash entre journal global et intention locale,
  double soumission concurrente, pièce modifiée, formats, limites, séparation entre comptes.
- MCP et CLI exposent tout ce parcours. README, guide des demandes et catalogue actualisés.
- `reviews.py` et `review_delivery.py` : réexamens liés au compte, pièces figées, import avec
  blocage de l'ancien dépôt, contrôle local, export et soumission par révision. Journal global
  contre les doublons, récupération après coupure, relecture du statut, des pièces et du PDF
  officiel. Guide dédié : `docs/REVIEWS.md`.
- `cfe_submit_review` et `cfe_get_review(refresh=true)` exposent l'envoi et la vérification.
  Tests de navigateur avec toutes les requêtes interceptées ; aucune nouvelle création réelle
  pour tester l'adaptateur. L'envoi réel antérieur utilisait le script supervisé privé.
- Audit du nouveau dépôt : les 88 formulaires correspondent à l'asset Android ; aucun champ
  de formulaire non mappé. Les paramètres des quatre actions d'écriture concordent avec le
  schéma Dataverse. Voir `docs/NEW_CLAIMS_AUDIT.md` pour les preuves et les limites.
- `documents.py` : politique obligatoire `metadata-v1`, copies privées, empreintes distinctes,
  préservation des originaux et blocage si le nettoyage n'est pas vérifiable. `docs/METADATA.md`.

Les écritures de nouveaux soins par l'API mobile restent non testées en production. Ce dossier
concernait un soin déjà déclaré et a été envoyé comme réexamen via la messagerie du portail.
Le MCP expose maintenant la préparation, la soumission générale et la vérification distante
des réexamens à partir du parcours observé. Le script supervisé, le journal et les pièces sont
privés, hors dépôt de code. L'ancien brouillon de soins reste bloqué pour éviter un nouveau
dépôt de ce dossier. Ne pas renvoyer ce réexamen.

La vérification de bout en bout du dépôt nécessitera une demande adaptée à ce parcours et une
instruction de transmission du titulaire. Ne pas utiliser les exemples synthétiques des tests
sur le serveur réel. En cas de réponse ambiguë, ne pas supprimer le journal ni répéter l'envoi.

Les désassemblages et preuves brutes sont hors dépôt dans
`~/.local/share/cfe-mcp/research/android/`. Ne pas afficher les constantes techniques ni les jetons.
