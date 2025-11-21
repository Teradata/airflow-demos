# Airbyte ACA (Automated Connection Agent) System

## Author:
**Satish Chinthanippu** – satish.chinthanippu@example.com

## Product Group Alignment:
Customer Developer Tools

---

## Agent Description

This agent automates the end-to-end creation of Airbyte sources, destinations, and connections using a combination of:

- **RAG (Retrieval-Augmented Generation)**
- **Embedding models (amazon.titan-embed-text-v2)**
- **LLM (ChatBedrock via Inference Profile ARN)**
- **Chroma vector database**

It supports two operational modes:

### **1. Connected Mode (Airbyte instance reachable)**
The agent automatically:
- Creates the **source**  
- Creates the **destination**  
- Creates the **connection**  
- Selects appropriate streams  
- Optionally triggers a sync job  

This makes Airbyte configuration fully automated.

### **2. Disconnected Mode (No Airbyte instance or placeholders detected)**
The agent **does NOT call Airbyte** and instead generates a **connection bundle** containing all connection metadata.  
This bundle can be:
- Saved  
- Versioned  
- Edited  
- Re-applied later  
- Passed to other engineers or CI/CD pipelines  

This mode is safe for:
- Running locally  
- Running without credentials  
- Sharing connection definitions  
- Pre-deployment validation  

---

# 📦 How Connector Specs Are Indexed in Vector DB

The companion script `index_manager.py` loads Airbyte connector specifications into a vector database.

### Steps:
1. **Extract connector files** such as  
   - `_spec.json`  
   - `metadata.yaml`  
   - Documentation / README  

2. **Chunk and embed** using `amazon.titan-embed-text-v2`.

3. **Store embeddings in Chroma**, including metadata like:
   - Connector name  
   - Definition ID  
   - Field-level configuration  
   - Supported sync modes  

4. These embeddings allow semantic matching between user intent and connector specifications.

This enables natural language interaction like:

> “Sync customers table from Postgres to Teradata every 6 hours.”

The agent retrieves ALL relevant documentation and uses it to generate the configuration.

---

# 🤖 How Automated Connection Generation Works

When a user runs:

```
python generate_connection.py --query "sync customers table from postgres to teradata"
```

the agent performs:

### **1. Retrieval**
Semantic vector search finds:
- Postgres spec  
- Teradata spec  
- Required fields  
- Sync capabilities  
- Example configurations  

### **2. Prompt Assembly**
A structured template is filled with:
- Retrieved documentation  
- User intent  
- Workspace ID 

### **3. LLM Generation**
The LLM returns exactly one JSON object called:

```
connection_bundle
```

with keys:
- source  
- destination  
- sync settings  
- metadata  

### **4. Validate or Skip Airbyte Calls**
- If placeholders exist → skip Airbyte API → return bundle  
- If Airbyte reachable & bundle complete → create live Airbyte resources  

### **5. Optional Sync Trigger**
If `--run-sync` is passed or env variable `RUN_SYNC=true`, a sync job starts immediately.

---

# 📦 What is a Connection Bundle?

A **bundle** is a portable JSON file containing all Airbyte connection details:
- Source configuration  
- Destination configuration  
- Sync configuration  
- Selected streams  
- Metadata  

It enables:
- Reusability  
- Version control  
- Cross-environment deployment  
- Secure handling (placeholders instead of secrets)  

### Example bundle usage:

#### Generate only:
```
python generate_connection.py --query "sync customers" --save-bundle cust.json
```

#### Apply bundle later:
```
python generate_connection.py --apply-bundle cust.json --create
```

#### Apply & immediately sync:
```
python generate_connection.py --apply-bundle cust.json --create --run-sync
```

Bundles allow customers or engineers to separate:
- **Generation**
- **Validation**
- **Execution**

---

## Status

### **Achieved**
✔ **RAG-powered natural-language connection generation**  
✔ **Connector-aware generation** using embedded Postgres + Teradata specifications  
✔ **Placeholder-safe operation** (auto-switch to generate-only mode)  
✔ **Airbyte API integration** for creating sources, destinations, and connections  
✔ **Bundle generation (`--save-bundle`)**  
✔ **Bundle application (`--apply-bundle`)**  
✔ **Sync triggering (`--run-sync`)**  
✔ **Incremental and full index rebuild** in `index_manager.py`  
✔ **Metadata sanitization** for clean vector storage  
✔ **Atomic index swap** to avoid partial updates  
✔ **Reliable, error-free behavior** for Postgres source → Teradata destination flows, as these are currently indexed  
✔ **Supports offline/disconnected usage** via bundle-only generation

### **Current Limitations**
⚠ **Only Postgres (source) and Teradata (destination) connectors are indexed today**  
— Therefore, the agent reliably generates and applies connections *only* for these connectors.  
— Other connectors (S3, MySQL, BigQuery, Kafka, etc.) may produce incomplete or invalid configurations because their documentation has not yet been embedded into the vector DB.

### **Remaining Work**
- 📌 **Index additional Airbyte connectors** (full OSS catalog)  
- 📌 **Improve dynamic table/stream inference** from user intent  
- 📌 **Introduce versioned bundles**  
  - Semantic versioning of bundles  
  - Automatic commit to Git/GitHub  
  - Ability to “pull” or “apply” bundle versions  
- 📌 **GitHub pipeline integration**  
  - Push bundles to repos  
  - Apply bundles automatically in CI/CD  
- 📌 **Bundle schema validator** (`--validate-bundle`)  
- 📌 **UI wrapper / lightweight dashboard**  
- 📌 **Add missing unit tests for generator & index manager**  
- 📌 **Support multi-connector workflows** once all connectors are indexed  
- 📌 **Add connector auto-discovery from metadata.yaml** for faster onboarding

---

## Technology Components and Dependencies

### Languages
- Python 3.9+

### AI Models
- AWS Bedrock:
  - Anthropic Claude (LLM)
  - Titan Embeddings (amazon.titan-embed-text-v2:0)

### Frameworks / Libraries
- LangChain  
- ChromaDB  
- Requests  
- PyYAML  
- dotenv  

### Other Components
- Airbyte API  

---

## Value Description

### Highest Value Category: **Productivity**

### Frequency of Use
10–100 times per day depending on automation workflows.

### Cost to Run
- LLM: low cost (fractions of a cent to a few cents)
- Embeddings: negligible
- Local compute: minimal

### Productivity Savings Example
Manual Airbyte connection creation ≈ **15 minutes**.

If a team creates **200 connections/year**:
- 200 × 0.25 hours = **50 hours saved**
- At $150/hr → **$7,500 saved per team annually**

Also improves consistency and reduces human error.

---

## Author’s Notes

### Key Learnings
- Understanding and implementing Retrieval-Augmented Generation (RAG)
- Building Agentic RAG systems for automated decision-making
- Applying vector database concepts for high‑quality retrieval
- Designing end-to-end data automation using Airbyte APIs
- Leveraging AWS Bedrock’s Inference Profile ARN for production-grade LLM calls
- Creating bundles for reproducible connection setups
- Managing incremental and full index rebuild flows
- Understanding Airbyte connection lifecycle and stream metadata
- Building placeholder-safe workflows for secure automation
- Handling multi-connector schemas such as Postgres and Teradata

### Skills Gained
- Expertise in LLM orchestration (LangChain + AWS Bedrock)
- Practical vector DB skills (ChromaDB, embeddings, metadata storage)
- Experience with embeddings, text-splitters, and chunk tuning
- Advanced Python programming and module structuring
- Designing CLIs with flexible runtime modes
- Working with Airbyte public API endpoints
- Automating ETL/ELT workflows through AI
- Strong debugging skills for multi-service workflows
- Building reusable bundle formats for CI/CD and environment provisioning

---

# Deployment Instructions

## 1. Install Dependencies
```bash
pip install -r requirements.txt

```

---

## 2. Configure Environment Variables

This project includes:

```
.env.template
```

Copy it and fill values:

```bash
cp .env.template .env
```

Then update variables such as:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`
- `BEDROCK_LLM_ARN`
- `BEDROCK_PROVIDER`
- `EMBED_MODEL_ID`
- `AIRBYTE_API_URL`
- `AIRBYTE_API_TOKEN`
- `AIRBYTE_WORKSPACE_ID`
- And feature toggles like `RUN_SYNC=true` and `CREATE_IN_AIRBYTE`

## 3. Build RAG Index (Index Manager)

### Full rebuild:
```bash
python index_manager.py   --docs-dir ./airbyte_docs   --persist-root ./chroma_store   --rebuild   --smoke-query postgres   --smoke-query teradata
```

### Incremental update:
```bash
python index_manager.py   --docs-dir ./airbyte_docs   --persist-root ./chroma_store   --incremental
```

---

## 4. Generate a Connection Bundle

```bash
python generate_connection.py   --query "Sync customers table from Postgres to Teradata using TD2 auth"   --save-bundle bundle.json
```

---

### > **ℹ️ Note: Validate & Update `bundle.json` Before Applying**

> Before running the `--apply-bundle` command, make sure to:
>
> 1. **Open the generated `bundle.json`.**
> 2. **Replace all placeholders** such as:
>    - `<<HOST>>`  
>    - `<<USERNAME>>`  
>    - `<<PASSWORD>>`  
>    - `<<SOURCE_DEFINITION_ID>>`  
>    - `<<DEST_DEFINITION_ID>>`  
>    - Any other `<<...>>` tokens
> 3. **Verify all required connection fields** (source, destination, sync) have valid, non-placeholder values.
>
> After replacing all placeholders with real values, apply the bundle.

## 5. Apply the Bundle

```bash
python generate_connection.py --apply-bundle bundle.json --create
```

---

## 6. Apply + Trigger Sync

```bash
python generate_connection.py --apply-bundle bundle.json --create --run-sync
```

---

---

# 📘 Usage Examples (Real Queries & Behavior)

Below are real scenarios showing how the agent behaves with different queries.

---

## 1️⃣ Basic Bundle Generation (Placeholders Only)

### Command
```bash
python generate_connection.py --query "sync customers from postgres to S3"
```

### What Happens
- RAG retrieves Postgres + S3 documentation from vector DB.
- LLM generates a **connection_bundle** using placeholders for secrets.
- No Airbyte API call occurs (placeholder-safe mode).
- Outputs bundle JSON directly to terminal.

---

## 2️⃣ Selective Stream Bundle Generation

### Command
```bash
python generate_connection.py   --query "Generate a connection_bundle for Postgres->S3. Only include streams named 'customers' and 'customer_profiles'; prefer 'customers' if both exist. Use placeholders for secrets. Output JSON only."   --save-bundle selective_streams.json
```

### What Happens
- Generates bundle including only selected streams.
- Writes JSON to `selective_streams.json`.
- Safe mode activated if placeholders exist.

---

## 3️⃣ Postgres → Teradata With User-Provided Details

### Command
```bash
python generate_connection.py   --query "Generate an Airbyte connection_bundle to sync the 'customers' table from a Postgres source to a Teradata destination. Postgres details: host=xxx, database=testdb, username=xx, password=xx. Teradata details: host=tt, username=tt, password=tt include a Teradata logmech object with auth_type TD2 using same username and password placeholders. Use placeholders for secrets and output only JSON. Include metadata.requested_streams=['customers']."   --save-bundle customers_pg_td_bundle.json
```

### What Happens
- LLM constructs a valid Postgres + Teradata connection bundle.
- Includes proper Teradata `logmech` object.
- Creates the source, destination, connection, and triggers a sync job when an Airbyte instance is available; otherwise, saves the generated bundle to customers_pg_td_bundle.json

---

## 4️⃣ Postgres → Teradata With Custom Schema

### Command
```bash
python generate_connection.py   --query "Generate an Airbyte connection_bundle to sync the 'customers' table from a Postgres source to a Teradata destination. Postgres details: host=psfsf, database=3, username=3, password=3. Teradata details: host=pdd, username=dd, password=3, schema=airbyte_agent include a Teradata logmech object with auth_type TD2 using same username and password placeholders. Include metadata.requested_streams=['customers']."   --save-bundle customers_pg_td_bundle.json
```

### What Happens
- Same generation as above but with schema=airbyte_agent.
- Creates the source, destination, connection, and triggers a sync job when an Airbyte instance is available; otherwise, saves the generated bundle to customers_pg_td_bundle.json.

---

## 5️⃣ Postgres → Teradata With Minimal/Messy Details

### Command
```bash
python generate_connection.py   --query "Generate an Airbyte connection_bundle to sync the customers table from a Postgres source to a Teradata destination. Postgres details: host=22, database=2, username=2, password=22. Teradata details: host=ss, username=dd, password=d, schema=airbyte_agent include a Teradata logmech object with auth_type TD2 using same username and password placeholders."   --save-bundle customers_pg_td_bundle.json
```

### What Happens
- Even if user input is messy or inconsistent, the agent:
  - normalizes structure
  - generates correct logmech
  - sets placeholders where required
- Saves bundle safely.

---

## Additional Notes
- Bundle files are portable.
- Placeholder detection prevents accidental API execution.
- Index Manager should be run before Connection Generator.
- Connection Generator can be used by customers & engineers.
