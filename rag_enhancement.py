"""
RAG Enhancement for PGA Agent
Adds retrieval-augmented generation capabilities for smarter dbt model creation
"""

import logging
import json
import asyncio
from typing import Dict, List, Optional, Any
from pathlib import Path
import numpy as np
from dataclasses import dataclass, asdict

# Vector database (you can choose: ChromaDB, Pinecone, Weaviate, FAISS)
try:
    import chromadb
    from chromadb.config import Settings
    VECTOR_DB_AVAILABLE = True
except ImportError:
    VECTOR_DB_AVAILABLE = False
    print("⚠️ ChromaDB not installed. Install with: pip install chromadb")

# Embeddings (you can choose: OpenAI, HuggingFace, local models)
try:
    from sentence_transformers import SentenceTransformer
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    print("⚠️ SentenceTransformers not installed. Install with: pip install sentence-transformers")

logger = logging.getLogger(__name__)

@dataclass
class SchemaContext:
    """Schema metadata with business context"""
    table_name: str
    database: str
    description: str
    business_domain: str
    columns: List[Dict[str, Any]]
    sample_queries: List[str] = None
    related_tables: List[str] = None
    business_rules: List[str] = None

@dataclass
class BusinessPattern:
    """Reusable business pattern for model generation"""
    pattern_name: str
    description: str
    use_cases: List[str]
    sql_template: str
    required_tables: List[str]
    business_logic: str
    confidence_score: float = 0.0

class RAGEnhancedPGA:
    """
    RAG-enhanced Planning & Generation Agent
    Combines schema discovery with intelligent context retrieval
    """
    
    def __init__(self, enable_rag: bool = True):
        self.enable_rag = enable_rag and VECTOR_DB_AVAILABLE and EMBEDDINGS_AVAILABLE
        
        if self.enable_rag:
            self._initialize_rag_components()
            logger.info("✅ RAG enhancement enabled")
        else:
            logger.info("⚪ RAG enhancement disabled - missing dependencies or disabled")
    
    def _initialize_rag_components(self):
        """Initialize vector database and embedding model"""
        try:
            # Initialize ChromaDB
            self.chroma_client = chromadb.PersistentClient(
                path="./rag_knowledge_base"
            )
            
            # Create collections for different types of knowledge
            self.schema_collection = self.chroma_client.get_or_create_collection(
                name="schema_knowledge",
                metadata={"description": "Database schemas and table context"}
            )
            
            self.pattern_collection = self.chroma_client.get_or_create_collection(
                name="business_patterns", 
                metadata={"description": "Reusable dbt patterns and templates"}
            )
            
            self.rules_collection = self.chroma_client.get_or_create_collection(
                name="business_rules",
                metadata={"description": "Domain-specific business logic and rules"}
            )
            
            # Initialize embedding model (lightweight, works offline)
            self.embedder = SentenceTransformer('all-MiniLM-L6-v2')
            
        except Exception as e:
            logger.error(f"Failed to initialize RAG components: {e}")
            self.enable_rag = False
    
    async def index_schema_knowledge(self, schemas: List[SchemaContext]):
        """Add schema knowledge to vector database"""
        if not self.enable_rag:
            return
            
        documents = []
        metadatas = []
        ids = []
        
        for schema in schemas:
            # Create searchable document
            doc_text = f"""
            Table: {schema.table_name}
            Database: {schema.database}
            Description: {schema.description}
            Business Domain: {schema.business_domain}
            Columns: {', '.join([col.get('name', '') for col in schema.columns])}
            """
            
            documents.append(doc_text)
            metadatas.append({
                "table_name": schema.table_name,
                "database": schema.database,
                "business_domain": schema.business_domain,
                "column_count": len(schema.columns)
            })
            ids.append(f"{schema.database}.{schema.table_name}")
        
        # Add to vector database
        embeddings = self.embedder.encode(documents).tolist()
        
        self.schema_collection.add(
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
            ids=ids
        )
        
        logger.info(f"✅ Indexed {len(schemas)} schemas in RAG knowledge base")
    
    async def find_relevant_schemas(self, business_prompt: str, limit: int = 10) -> List[Dict]:
        """Find schemas relevant to business requirements using RAG"""
        if not self.enable_rag:
            return []
        
        try:
            # Create embedding for business prompt
            query_embedding = self.embedder.encode([business_prompt]).tolist()[0]
            
            # Search for relevant schemas
            results = self.schema_collection.query(
                query_embeddings=[query_embedding],
                n_results=limit,
                include=['documents', 'metadatas', 'distances']
            )
            
            relevant_schemas = []
            for i, (doc, metadata, distance) in enumerate(zip(
                results['documents'][0], 
                results['metadatas'][0], 
                results['distances'][0]
            )):
                relevant_schemas.append({
                    'table_name': metadata['table_name'],
                    'database': metadata['database'],
                    'business_domain': metadata['business_domain'],
                    'relevance_score': 1.0 - distance,  # Convert distance to similarity
                    'context': doc
                })
            
            logger.info(f"🔍 Found {len(relevant_schemas)} relevant schemas via RAG")
            return relevant_schemas
            
        except Exception as e:
            logger.error(f"RAG schema search failed: {e}")
            return []
    
    async def add_business_pattern(self, pattern: BusinessPattern):
        """Add a reusable business pattern to the knowledge base"""
        if not self.enable_rag:
            return
            
        doc_text = f"""
        Pattern: {pattern.pattern_name}
        Description: {pattern.description}
        Use Cases: {', '.join(pattern.use_cases)}
        Business Logic: {pattern.business_logic}
        Required Tables: {', '.join(pattern.required_tables)}
        """
        
        embedding = self.embedder.encode([doc_text]).tolist()[0]
        
        # ChromaDB Metadata Limitation Workaround:
        # ChromaDB doesn't natively support list/array types in metadata fields.
        # Lists must be JSON serialized to strings for storage, then deserialized 
        # when retrieved. This prevents issues with:
        # 1. Lists containing commas (which would break comma-separated storage)
        # 2. Complex nested data structures
        # 3. Empty lists or None values
        # 4. Unicode characters in list elements
        # See: https://docs.trychroma.com/reference/Collection#add
        pattern_metadata = {
            "pattern_name": pattern.pattern_name,
            "description": pattern.description,
            "use_cases": json.dumps(pattern.use_cases),  # JSON serialize list
            "business_logic": pattern.business_logic,
            "required_tables": json.dumps(pattern.required_tables),  # JSON serialize list
            "sql_template": pattern.sql_template,
            "confidence_score": float(pattern.confidence_score)
        }
        
        self.pattern_collection.add(
            embeddings=[embedding],
            documents=[doc_text],
            metadatas=[pattern_metadata],
            ids=[pattern.pattern_name]
        )
    
    async def find_similar_patterns(self, business_prompt: str, limit: int = 5) -> List[BusinessPattern]:
        """Find similar business patterns for model generation"""
        if not self.enable_rag:
            return []
        
        try:
            query_embedding = self.embedder.encode([business_prompt]).tolist()[0]
            
            results = self.pattern_collection.query(
                query_embeddings=[query_embedding],
                n_results=limit,
                include=['metadatas', 'distances']
            )
            
            patterns = []
            for metadata, distance in zip(results['metadatas'][0], results['distances'][0]):
                # Convert JSON metadata back to BusinessPattern format
                try:
                    pattern = BusinessPattern(
                        pattern_name=metadata['pattern_name'],
                        description=metadata['description'],
                        use_cases=json.loads(metadata['use_cases']),  # JSON deserialize list
                        sql_template=metadata['sql_template'],
                        required_tables=json.loads(metadata['required_tables']),  # JSON deserialize list
                        business_logic=metadata['business_logic'],
                        confidence_score=1.0 - distance  # Set confidence based on similarity
                    )
                    patterns.append(pattern)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to deserialize pattern metadata: {e}")
                    continue
            
            logger.info(f"🎯 Found {len(patterns)} similar patterns via RAG")
            return patterns
            
        except Exception as e:
            logger.error(f"RAG pattern search failed: {e}")
            return []
    
    async def enhance_business_prompt(self, original_prompt: str) -> str:
        """Enhance business prompt with relevant context from RAG"""
        if not self.enable_rag:
            return original_prompt
        
        # Find relevant schemas and patterns
        relevant_schemas = await self.find_relevant_schemas(original_prompt, limit=5)
        similar_patterns = await self.find_similar_patterns(original_prompt, limit=3)
        
        # Build enhanced context
        enhanced_prompt = f"BUSINESS REQUIREMENT: {original_prompt}\n\n"
        
        if relevant_schemas:
            enhanced_prompt += "RELEVANT DATA SOURCES:\n"
            for schema in relevant_schemas[:3]:  # Top 3 most relevant
                enhanced_prompt += f"- {schema['database']}.{schema['table_name']} "
                enhanced_prompt += f"(relevance: {schema['relevance_score']:.2f})\n"
        
        if similar_patterns:
            enhanced_prompt += "\nSIMILAR PATTERNS:\n"
            for pattern in similar_patterns[:2]:  # Top 2 patterns
                enhanced_prompt += f"- {pattern.pattern_name}: {pattern.description}\n"
        
        logger.info("🚀 Enhanced business prompt with RAG context")
        return enhanced_prompt


# Pre-built business patterns to seed the RAG system
STANDARD_BUSINESS_PATTERNS = [
    BusinessPattern(
        pattern_name="customer_lifetime_value",
        description="Calculate customer lifetime value using purchase history and behavioral data",
        use_cases=["Customer analytics", "Marketing attribution", "Retention analysis"],
        sql_template="""
        SELECT 
            customer_id,
            SUM(order_total) / COUNT(DISTINCT order_date) as avg_order_value,
            COUNT(DISTINCT order_date) as purchase_frequency,
            AVG(days_between_orders) as avg_days_between_orders,
            SUM(order_total) * (365 / AVG(days_between_orders)) as estimated_clv
        FROM {{ ref('fct_orders') }}
        GROUP BY customer_id
        """,
        required_tables=["orders", "customers"],
        business_logic="CLV = Average Order Value × Purchase Frequency × Customer Lifespan"
    ),
    
    BusinessPattern(
        pattern_name="revenue_recognition",
        description="Monthly revenue recognition with proper accrual accounting",
        use_cases=["Financial reporting", "Revenue analytics", "Performance tracking"],
        sql_template="""
        SELECT 
            DATE_TRUNC('month', order_date) as revenue_month,
            SUM(CASE WHEN payment_status = 'completed' THEN order_total ELSE 0 END) as recognized_revenue,
            SUM(order_total) as total_bookings,
            COUNT(DISTINCT customer_id) as unique_customers
        FROM {{ ref('fct_orders') }}
        GROUP BY DATE_TRUNC('month', order_date)
        """,
        required_tables=["orders", "payments"],
        business_logic="Revenue recognized only when payment is completed and service delivered"
    ),
    
    BusinessPattern(
        pattern_name="cohort_analysis",
        description="User cohort analysis for retention and engagement tracking",
        use_cases=["User retention", "Product analytics", "Growth metrics"],
        sql_template="""
        WITH user_cohorts AS (
            SELECT 
                user_id,
                DATE_TRUNC('month', first_activity_date) as cohort_month
            FROM {{ ref('dim_users') }}
        )
        SELECT 
            cohort_month,
            activity_month,
            COUNT(DISTINCT user_id) as active_users,
            COUNT(DISTINCT user_id) / FIRST_VALUE(COUNT(DISTINCT user_id)) 
                OVER (PARTITION BY cohort_month ORDER BY activity_month) as retention_rate
        FROM user_cohorts
        GROUP BY cohort_month, activity_month
        """,
        required_tables=["users", "activity_events"],
        business_logic="Track user retention by grouping users into cohorts based on signup month"
    )
]


async def initialize_rag_system(rag_agent: RAGEnhancedPGA):
    """Initialize RAG system with standard business patterns"""
    if not rag_agent.enable_rag:
        return
    
    logger.info("🚀 Initializing RAG system with business patterns...")
    
    # Add standard patterns
    for pattern in STANDARD_BUSINESS_PATTERNS:
        await rag_agent.add_business_pattern(pattern)
    
    logger.info(f"✅ Added {len(STANDARD_BUSINESS_PATTERNS)} standard business patterns to RAG")


# Example usage
async def demo_rag_enhanced_pga():
    """Demonstrate RAG-enhanced PGA capabilities"""
    rag_agent = RAGEnhancedPGA()
    
    if rag_agent.enable_rag:
        # Initialize with standard patterns
        await initialize_rag_system(rag_agent)
        
        # Example: Enhance a business prompt
        business_prompt = "Build analytics for customer orders and revenue"
        enhanced_prompt = await rag_agent.enhance_business_prompt(business_prompt)
        
        print("📝 Original Prompt:", business_prompt)
        print("\n🚀 Enhanced Prompt:")
        print(enhanced_prompt)
        
        # Find relevant patterns
        patterns = await rag_agent.find_similar_patterns(business_prompt)
        print(f"\n🎯 Found {len(patterns)} relevant patterns")
        for pattern in patterns:
            print(f"   - {pattern.pattern_name} (confidence: {pattern.confidence_score:.2f})")


if __name__ == "__main__":
    asyncio.run(demo_rag_enhanced_pga())