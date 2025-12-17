# API SGA Web - Système de Gestion des Agents
# Version optimisée pour Railway

from fastapi import FastAPI, HTTPException, Depends, Query, Body, File, UploadFile, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import sqlite3
from datetime import date, datetime, timedelta
from calendar import monthrange
import os
import json
import csv
import tempfile
import io
from contextlib import contextmanager
import logging

# Configuration du logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration pour Railway vs Local
if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("PORT"):
    # Sur Railway/Cloud
    DATABASE_PATH = "/tmp/planning.db"
    BASE_DIR = "/tmp"
else:
    # En local
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATABASE_PATH = os.path.join(BASE_DIR, "database", "planning.db")

DATE_AFFECTATION_BASE = "2025-11-01"

# Initialisation de FastAPI
app = FastAPI(
    title="SGA Web API",
    description="API du Système de Gestion des Agents",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Configuration CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration des templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Modèles Pydantic
class AgentBase(BaseModel):
    code: str
    nom: str
    prenom: str
    code_groupe: str

class AgentCreate(AgentBase):
    pass

class AgentResponse(BaseModel):
    code: str
    nom: str
    prenom: str
    code_groupe: str
    date_entree: str
    statut: str

class PlanningRequest(BaseModel):
    mois: int
    annee: int

class ShiftModification(BaseModel):
    code_agent: str
    date: str
    shift: str

class AbsenceRequest(BaseModel):
    code_agent: str
    date: str
    type_absence: str  # C, M, A

class CongeRequest(BaseModel):
    code_agent: str
    date_debut: str
    date_fin: str

# Contexte de connexion à la base
@contextmanager
def get_db_connection():
    """Contexte pour la connexion à la base de données"""
    # S'assurer que le dossier existe
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
    
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def get_db_cursor():
    """Obtenir un curseur de base de données"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        yield cursor, conn

# Initialisation de la base
def init_database():
    """Initialise la base de données avec toutes les tables"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Table agents
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                code TEXT PRIMARY KEY,
                nom TEXT NOT NULL,
                prenom TEXT NOT NULL,
                code_groupe TEXT NOT NULL,
                date_entree TEXT,
                date_sortie TEXT,
                statut TEXT DEFAULT 'actif'
            )
        """)
        
        # Table planning
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS planning (
                code_agent TEXT,
                date TEXT,
                shift TEXT,
                origine TEXT,
                PRIMARY KEY (code_agent, date)
            )
        """)
        
        # Table jours_feries
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jours_feries (
                date TEXT PRIMARY KEY,
                description TEXT
            )
        """)
        
        # Insérer quelques agents d'exemple si base vide
        cursor.execute("SELECT COUNT(*) FROM agents")
        if cursor.fetchone()[0] == 0:
            agents_exemple = [
                ('AG001', 'Dupont', 'Jean', 'A', DATE_AFFECTATION_BASE),
                ('AG002', 'Martin', 'Pierre', 'B', DATE_AFFECTATION_BASE),
                ('AG003', 'Durand', 'Marie', 'C', DATE_AFFECTATION_BASE),
            ]
            cursor.executemany(
                "INSERT INTO agents (code, nom, prenom, code_groupe, date_entree) VALUES (?, ?, ?, ?, ?)",
                agents_exemple
            )
            logger.info("Base de données initialisée avec des données d'exemple")
        
        conn.commit()
        logger.info("Base de données initialisée avec succès")

# Fonctions utilitaires
JOURS_FRANCAIS = {
    'Mon': 'Lun', 'Tue': 'Mar', 'Wed': 'Mer', 'Thu': 'Jeu',
    'Fri': 'Ven', 'Sat': 'Sam', 'Sun': 'Dim'
}

def _cycle_standard_8j(jour_cycle):
    """Cycle de rotation 8 jours"""
    cycle = ['1', '1', '2', '2', '3', '3', 'R', 'R']
    return cycle[jour_cycle % 8]

def _get_decalage_standard(code_groupe):
    """Décalage par groupe"""
    decalages = {'A': 0, 'B': 2, 'C': 4, 'D': 6}
    return decalages.get(code_groupe.upper(), 0)

# ... [LES AUTRES FONCTIONS UTILITAIRES] ...
# Copiez ici toutes vos fonctions utilitaires originales

# Initialiser la base au démarrage
@app.on_event("startup")
async def startup_event():
    """Initialise la base de données au démarrage"""
    init_database()
    logger.info("API SGA Web démarrée")

# =========================================================================
# ENDPOINTS DE BASE
# =========================================================================

@app.get("/")
async def home(request: Request):
    """Page d'accueil avec interface web"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/health")
async def health_check():
    """Vérifie l'état de l'API et de la base"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            
        return {
            "status": "healthy",
            "database": "connected",
            "tables": len(tables),
            "timestamp": datetime.now().isoformat(),
            "environment": "railway" if os.getenv("RAILWAY_ENVIRONMENT") else "local"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

# =========================================================================
# GESTION DES AGENTS (simplifiée)
# =========================================================================

@app.get("/api/agents", response_model=List[Dict[str, Any]])
async def get_agents(
    groupe: Optional[str] = Query(None, description="Filtrer par groupe"),
    actif: bool = Query(True, description="Agents actifs seulement")
):
    """Récupère la liste des agents"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM agents"
            params = []
            
            if actif:
                query += " WHERE date_sortie IS NULL"
            else:
                query += " WHERE 1=1"
            
            if groupe:
                if "WHERE" in query:
                    query += " AND code_groupe = ?"
                else:
                    query += " WHERE code_groupe = ?"
                params.append(groupe.upper())
            
            query += " ORDER BY code_groupe, code"
            cursor.execute(query, params)
            
            agents = []
            for row in cursor.fetchall():
                agent = dict(row)
                agents.append(agent)
            
            return agents
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/agents")
async def create_agent(agent: AgentCreate):
    """Ajoute un nouvel agent"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Vérifier si l'agent existe déjà
            cursor.execute("SELECT code FROM agents WHERE code = ?", (agent.code.upper(),))
            if cursor.fetchone():
                raise HTTPException(status_code=400, detail=f"L'agent {agent.code} existe déjà")
            
            # Insérer le nouvel agent
            cursor.execute("""
                INSERT INTO agents (code, nom, prenom, code_groupe, date_entree)
                VALUES (?, ?, ?, ?, ?)
            """, (
                agent.code.upper(),
                agent.nom,
                agent.prenom,
                agent.code_groupe.upper(),
                DATE_AFFECTATION_BASE
            ))
            
            conn.commit()
            
            return {
                "success": True,
                "message": f"Agent {agent.code} créé avec succès"
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# PLANNING (simplifié)
# =========================================================================

@app.get("/api/planning/global/{mois}/{annee}")
async def get_planning_global(mois: int, annee: int):
    """Récupère le planning global pour un mois"""
    try:
        if not (1 <= mois <= 12):
            raise HTTPException(status_code=400, detail="Mois invalide (1-12)")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Nombre de jours dans le mois
            _, jours_mois = monthrange(annee, mois)
            
            # Récupérer les agents
            cursor.execute("""
                SELECT code, nom, prenom, code_groupe 
                FROM agents 
                WHERE date_sortie IS NULL 
                ORDER BY code_groupe, code
            """)
            agents = cursor.fetchall()
            
            # Préparer la réponse
            planning_data = []
            
            for agent in agents:
                code, nom, prenom, groupe = agent
                planning_data.append({
                    "code": code,
                    "nom": nom,
                    "prenom": prenom,
                    "groupe": groupe,
                    "nom_complet": f"{nom} {prenom}"
                })
            
            return {
                "mois": mois,
                "annee": annee,
                "total_jours": jours_mois,
                "total_agents": len(agents),
                "agents": planning_data,
                "message": "Planning généré avec succès"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# TABLEAU DE BORD
# =========================================================================

@app.get("/api/dashboard")
async def get_dashboard():
    """Statistiques pour le tableau de bord"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Agents par groupe
            cursor.execute("""
                SELECT code_groupe, COUNT(*) 
                FROM agents 
                WHERE date_sortie IS NULL 
                GROUP BY code_groupe
            """)
            groupes = {row[0]: row[1] for row in cursor.fetchall()}
            
            # Total agents
            total_agents = sum(groupes.values())
            
            return {
                "date": date.today().isoformat(),
                "total_agents": total_agents,
                "agents_par_groupe": groupes,
                "status": "success",
                "message": "Tableau de bord généré"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# IMPORT/EXPORT CSV (sans pandas)
# =========================================================================

@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...)):
    """Importe des agents depuis un fichier CSV"""
    try:
        # Lire le fichier CSV
        contents = await file.read()
        content_str = contents.decode('utf-8')
        
        resultats = {
            "importes": 0,
            "ignores": 0,
            "erreurs": []
        }
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Lire le CSV
            csv_reader = csv.reader(io.StringIO(content_str))
            
            for index, row in enumerate(csv_reader):
                if index == 0:  # Skip header
                    continue
                    
                try:
                    if len(row) < 4:
                        resultats["ignores"] += 1
                        continue
                        
                    code = str(row[0]).strip().upper() if row[0] else ""
                    nom = str(row[1]).strip() if row[1] else ""
                    prenom = str(row[2]).strip() if row[2] else ""
                    groupe = str(row[3]).strip().upper() if row[3] else ""
                    
                    if not code or not nom or not prenom or groupe not in ['A', 'B', 'C', 'D', 'E']:
                        resultats["ignores"] += 1
                        continue
                    
                    # Vérifier si existe
                    cursor.execute("SELECT code FROM agents WHERE code = ?", (code,))
                    
                    if cursor.fetchone():
                        # Mettre à jour
                        cursor.execute("""
                            UPDATE agents 
                            SET nom = ?, prenom = ?, code_groupe = ?, date_sortie = NULL 
                            WHERE code = ?
                        """, (nom, prenom, groupe, code))
                    else:
                        # Insérer
                        cursor.execute("""
                            INSERT INTO agents (code, nom, prenom, code_groupe, date_entree)
                            VALUES (?, ?, ?, ?, ?)
                        """, (code, nom, prenom, groupe, DATE_AFFECTATION_BASE))
                    
                    resultats["importes"] += 1
                    
                except Exception as e:
                    resultats["erreurs"].append(f"Ligne {index+1}: {str(e)}")
                    resultats["ignores"] += 1
            
            conn.commit()
        
        return resultats
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================================================
# CONFIGURATION
# =========================================================================

@app.get("/api/config")
async def get_config():
    """Récupère la configuration de l'application"""
    return {
        "nom": "SGA Web",
        "version": "2.0.0",
        "environnement": "railway" if os.getenv("RAILWAY_ENVIRONMENT") else "local",
        "date_affection_base": DATE_AFFECTATION_BASE,
        "timestamp": datetime.now().isoformat()
    }

# =========================================================================
# LANCEMENT DE L'APPLICATION
# =========================================================================

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    
    print("=" * 60)
    print("🚀 SGA Web API - Système de Gestion des Agents")
    print("=" * 60)
    print(f"🌐 Environnement: {'Railway' if os.getenv('RAILWAY_ENVIRONMENT') else 'Local'}")
    print(f"📁 Base de données: {DATABASE_PATH}")
    print(f"🔧 Host: {host}")
    print(f"🔧 Port: {port}")
    print(f"📚 Documentation: http://{host}:{port}/docs")
    print("=" * 60)
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info"
    )
