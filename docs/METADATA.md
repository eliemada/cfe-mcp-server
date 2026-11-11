# Métadonnées des documents transmis

La politique `metadata-v1` est obligatoire pour les justificatifs de soins, les justificatifs
bancaires et les exports et envois de réexamen. Aucun réglage ne permet d'envoyer l'original en cas
d'échec du nettoyage. Elle s'applique aussi aux brouillons créés avant son ajout.

## Originaux et copies d'envoi

Le MCP conserve la copie originale privée et son SHA-256. Il produit une autre copie locale,
vérifie son empreinte et utilise un nom de fichier neutre pour l'envoi. Les dates et noms
présents dans le nom d'origine ne sont donc pas repris comme nom de pièce distante.

La validation locale et l'export retournent `outbound_documents`, avec la politique, le chemin
de la copie, sa taille, son empreinte et l'empreinte de l'original. Ces données restent dans les
résultats locaux du MCP ; elles ne sont pas insérées dans le document transmis.

Le transport de soins utilise uniquement les octets nettoyés. Le parcours bancaire utilise
également ces octets et le nom neutre pour son champ de fichier. Les journaux conservent la
description exacte de la copie proposée à l'envoi. Une pièce déjà téléversée avant cette
politique bloque un nouveau dépôt si son nettoyage n'est pas attesté dans le journal : il faut
la retirer puis téléverser sa copie nettoyée. Les anciens envois ne sont pas modifiés à distance.

## Nettoyage effectué

- PDF : dictionnaire Info complet (auteur, titre, dates, logiciel, etc.), XMP du document et
  des objets, informations privées d'applications, chemins de source connus, dates et auteur
  des annotations, miniatures et index auxiliaires. Une réécriture complète élimine les objets
  devenus inutiles et les anciennes révisions incrémentales. L'identifiant source est remplacé
  par un identifiant technique fixe.
- JPEG : segments EXIF, XMP, IPTC, commentaires et données après la fin de l'image. Les scans
  JPEG sont conservés sans recompression lorsque l'orientation et le profil couleur le
  permettent. Les JPEG contenus dans les PDF sont nettoyés aussi, y compris derrière les
  filtres Flate, ASCII85 et ASCIIHex.
- PNG : reconstruction à partir des pixels, sans champs texte, EXIF, XMP, dates ni blocs privés.
  L'orientation est appliquée avant suppression. Les images utilisant un profil couleur sont
  converties en sRGB avant suppression du profil ; les JPEG concernés deviennent des PNG.

Les champs techniques nécessaires au fichier restent présents : version PDF, nombre et taille
des pages, polices, espaces couleur, dimensions des images et paramètres de décodage. Le contenu
visible, le texte du dossier et les renseignements saisis dans les formulaires CFE ne sont pas
anonymisés. Ce nettoyage n'est pas une recherche de données dissimulées dans le contenu graphique.

## Cas bloqués

Les PDF avec signature numérique certifiée ne sont pas modifiés : une réécriture invaliderait
leur signature. Une image de signature manuscrite fait partie du contenu visible et est
conservée. Les PDF chiffrés ou contenant d'autres fichiers incorporés, les images animées et
les encodages non pris en charge sont refusés avant transmission. Les fichiers incorporés doivent
être extraits et joints séparément pour être nettoyés eux aussi.

La limite de taille est vérifiée après nettoyage : 3 Mio pour les pièces, 3 Mo pour le portail
bancaire. Une conversion qui dépasse la limite produit une erreur ; l'original n'est pas envoyé
à sa place. Une copie nettoyée modifiée localement bloque également l'envoi.

## Vérification

Les tests contiennent de vrais PDF synthétiques avec métadonnées, des JPEG progressifs, des
images orientées, des PNG transparents et des PDF contenant des JPEG à filtres composés.
Ils contrôlent les octets effectivement transmis par les adaptateurs simulés, l'intégrité des
pixels, le traitement des signatures et l'absence de contrôle serveur des soins après un échec.

Pour les trois pièces du réexamen préparé le 11 novembre 2026 : contrôle indépendant ExifTool
des champs d'identification, comparaison pixel par pixel de chaque page rendue à 144 dpi, et
comparaison du texte extrait. Les trois copies ont passé ces contrôles. Les preuves et originaux
restent dans le dossier privé du titulaire.

Références : [métadonnées PDF dans pikepdf](https://pikepdf.readthedocs.io/en/latest/topics/metadata.html),
[opérations de nettoyage](https://pikepdf.readthedocs.io/en/latest/api/sanitize.html),
[orientation des images dans Pillow](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html).
