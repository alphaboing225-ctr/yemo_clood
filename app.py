import os
import hashlib
import secrets
from dotenv import load_dotenv

load_dotenv(override=True)

from flask import Flask, render_template, request, session, redirect, jsonify
import firebase_admin
from firebase_admin import credentials, auth, firestore
from google.api_core.exceptions import AlreadyExists
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
# À définir dans le fichier .env : SECRET_KEY=une-longue-valeur-aleatoire
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024   # 20 Mo maximum par envoi

# Limite le nombre de requêtes par adresse IP (protège la route externe)
limiter = Limiter(get_remote_address, app=app, default_limits=[])

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

CHEMIN_CLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase-cle.json")
firebase_admin.initialize_app(credentials.Certificate(CHEMIN_CLE))
db = firestore.client()


# ---------------------------------------------------------------- Utilitaires

def projet_de_l_utilisateur(uid):
    doc = db.collection("projets").document(uid).get()
    return doc.to_dict() if doc.exists else None


def fichiers_de_l_utilisateur(uid):
    docs = (
        db.collection("projets").document(uid).collection("fichiers")
        .order_by("cree_le", direction=firestore.Query.DESCENDING)
        .stream()
    )
    return [d.to_dict() for d in docs]


def enregistrer_fichier(uid, projet, fichier):
    """Envoie le fichier sur Cloudinary puis l'enregistre dans Firestore.
    Retourne (dictionnaire_reponse, code_http)."""
    if not fichier or not fichier.filename:
        return {"erreur": "Aucun fichier reçu."}, 400
    if not (fichier.mimetype.startswith("image/") or fichier.mimetype.startswith("video/")):
        return {"erreur": "Seules les images et les vidéos sont acceptées."}, 400

    nom_projet = projet.get("nom", "")

    # La clé n'est volontairement PAS stockée dans les métadonnées Cloudinary
    try:
        resultat = cloudinary.uploader.upload(
            fichier.stream,
            folder="yemocloud/" + uid,
            resource_type="auto",
            context={"projet": nom_projet},
        )
    except Exception as e:
        return {"erreur": "Envoi à Cloudinary impossible : " + str(e)}, 500

    try:
        db.collection("projets").document(uid).collection("fichiers").add({
            "nom": fichier.filename,
            "url": resultat["secure_url"],
            "public_id": resultat["public_id"],
            "type": resultat.get("resource_type"),
            "taille": resultat.get("bytes"),
            "projet": nom_projet,
            "cree_le": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        return {"erreur": "Fichier envoyé mais non enregistré : " + str(e)}, 500

    return {
        "ok": True,
        "url": resultat["secure_url"],
        "nom": fichier.filename,
        "type": resultat.get("resource_type"),
    }, 200


# ---------------------------------------------------------------- Erreurs

@app.errorhandler(413)
def fichier_trop_gros(e):
    return jsonify({"erreur": "Fichier trop volumineux (20 Mo maximum)."}), 413


@app.errorhandler(429)
def trop_de_requetes(e):
    return jsonify({"erreur": "Trop de requêtes, réessaie dans un instant."}), 429


# ---------------------------------------------------------------- Pages

@app.route("/")
def accueil():
    return render_template("index.html")


@app.route("/connexion")
def connexion():
    return render_template("connexion.html")


@app.route("/session", methods=["POST"])
def creer_session():
    token = (request.get_json() or {}).get("token")
    try:
        infos = auth.verify_id_token(token)
    except Exception:
        return jsonify({"erreur": "token invalide"}), 401
    session["uid"] = infos["uid"]
    session["email"] = infos.get("email")
    return jsonify({"ok": True})


@app.route("/tableau-de-bord")
def tableau_de_bord():
    if "uid" not in session:
        return redirect("/connexion")
    h = hashlib.sha256(session["uid"].encode()).hexdigest()
    nom = "Yemo-" + h[:6].upper()
    teinte = int(h[6:9], 16) % 360
    projet = projet_de_l_utilisateur(session["uid"])
    fichiers = fichiers_de_l_utilisateur(session["uid"]) if projet else []
    return render_template(
        "tableau_de_bord.html",
        nom=nom,
        initiale=nom[0],
        teinte=teinte,
        email=session["email"],
        projet=projet,
        cle=projet.get("cle") if projet else None,
        fichiers=fichiers,
    )


@app.route("/nouveau-projet")
def nouveau_projet():
    if "uid" not in session:
        return redirect("/connexion")
    if projet_de_l_utilisateur(session["uid"]):
        return redirect("/tableau-de-bord")
    return render_template("nouveau_projet.html")


# ---------------------------------------------------------------- API (session)

@app.route("/api/creer-projet", methods=["POST"])
def creer_projet():
    if "uid" not in session:
        return jsonify({"erreur": "non connecté"}), 401

    donnees = request.get_json() or {}
    nom = (donnees.get("nom") or "").strip()
    if not nom or len(nom) > 50:
        return jsonify({"erreur": "Nom invalide (1 à 50 caractères)."}), 400

    cle = "yc_" + secrets.token_urlsafe(24)

    ref = db.collection("projets").document(session["uid"])
    try:
        ref.create({
            "nom": nom,
            "proprietaire_uid": session["uid"],
            "proprietaire_email": session.get("email"),
            "cle": cle,
            "cree_le": firestore.SERVER_TIMESTAMP,
        })
    except AlreadyExists:
        return jsonify({"erreur": "Ton compte a déjà un projet."}), 409
    except Exception as e:
        return jsonify({"erreur": "Enregistrement impossible : " + str(e)}), 500

    return jsonify({"ok": True})


@app.route("/api/televerser", methods=["POST"])
def televerser():
    if "uid" not in session:
        return jsonify({"erreur": "non connecté"}), 401
    projet = projet_de_l_utilisateur(session["uid"])
    if not projet:
        return jsonify({"erreur": "Crée d'abord un projet."}), 400

    corps, code = enregistrer_fichier(session["uid"], projet, request.files.get("fichier"))
    return jsonify(corps), code


# ---------------------------------------------------------------- API externe (nom + clé)

@app.route("/api/v1/televerser", methods=["POST"])
@limiter.limit("20 per minute")
def televerser_externe():
    nom_projet = (request.form.get("projet") or "").strip()
    cle = request.headers.get("X-Cle-Projet") or request.form.get("cle") or ""

    if not nom_projet or not cle:
        return jsonify({"erreur": "Le nom du projet et la clé sont requis."}), 400

    # Même message dans tous les cas d'échec, pour ne rien révéler
    echec = (jsonify({"erreur": "Projet ou clé invalide."}), 401)

    docs = db.collection("projets").where("cle", "==", cle).limit(1).get()
    if not docs:
        return echec

    doc = docs[0]
    projet = doc.to_dict()

    if projet.get("nom", "") != nom_projet:
        return echec

    # doc.id est l'uid du propriétaire : le fichier apparaîtra dans sa galerie
    corps, code = enregistrer_fichier(doc.id, projet, request.files.get("fichier"))
    return jsonify(corps), code


@app.route("/deconnexion")
def deconnexion():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    # Mets debug=False en production
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")