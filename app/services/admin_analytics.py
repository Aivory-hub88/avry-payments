"""
Admin Analytics Service
Provides analytics and aggregation for admin dashboard.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from app.database.db_service import db


class AdminAnalyticsService:
    """Service for aggregating admin analytics data"""
    
    @staticmethod
    async def get_diagnostic_stats(
        days: int = 30,
        tier_filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get diagnostic statistics for admin dashboard.
        
        Args:
            days: Number of days to include in stats
            tier_filter: Optional tier filter (free, snapshot, blueprint, enterprise)
            
        Returns:
            Dict with aggregated statistics
        """
        try:
            # Get diagnostics from past N days
            since_date = datetime.utcnow() - timedelta(days=days)
            
            # Query diagnostics
            query = f"""
                SELECT 
                    COUNT(*) as total_runs,
                    AVG(score) as average_score,
                    MIN(score) as min_score,
                    MAX(score) as max_score,
                    COUNT(CASE WHEN score >= 80 THEN 1 END) as advanced_count,
                    COUNT(CASE WHEN score >= 60 AND score < 80 THEN 1 END) as established_count,
                    COUNT(CASE WHEN score >= 40 AND score < 60 THEN 1 END) as emerging_count,
                    COUNT(CASE WHEN score < 40 THEN 1 END) as foundational_count
                FROM diagnostics
                WHERE created_at >= '{since_date.isoformat()}'
            """
            
            if tier_filter:
                query += f" AND tier = '{tier_filter}'"
            
            result = await db.execute_query(query)
            
            if result and len(result) > 0:
                row = result[0]
                return {
                    "total_runs": row.get("total_runs", 0),
                    "average_score": float(row.get("average_score", 0)) if row.get("average_score") else 0,
                    "min_score": row.get("min_score", 0),
                    "max_score": row.get("max_score", 100),
                    "category_distribution": {
                        "advanced": row.get("advanced_count", 0),
                        "established": row.get("established_count", 0),
                        "emerging": row.get("emerging_count", 0),
                        "foundational": row.get("foundational_count", 0)
                    }
                }
            
            return {
                "total_runs": 0,
                "average_score": 0,
                "min_score": 0,
                "max_score": 100,
                "category_distribution": {
                    "advanced": 0,
                    "established": 0,
                    "emerging": 0,
                    "foundational": 0
                }
            }
            
        except Exception as e:
            print(f"Error getting diagnostic stats: {e}")
            return {
                "total_runs": 0,
                "average_score": 0,
                "min_score": 0,
                "max_score": 100,
                "category_distribution": {
                    "advanced": 0,
                    "established": 0,
                    "emerging": 0,
                    "foundational": 0
                }
            }
    
    @staticmethod
    async def get_user_stats() -> Dict[str, Any]:
        """
        Get user account statistics.
        
        Returns:
            Dict with user count by tier/account type
        """
        try:
            query = """
                SELECT 
                    account_type,
                    COUNT(*) as count
                FROM users
                GROUP BY account_type
            """
            
            result = await db.execute_query(query)
            
            stats = {
                "total_users": 0,
                "by_account_type": {}
            }
            
            if result:
                for row in result:
                    account_type = row.get("account_type", "unknown")
                    count = row.get("count", 0)
                    stats["by_account_type"][account_type] = count
                    stats["total_users"] += count
            
            return stats
            
        except Exception as e:
            print(f"Error getting user stats: {e}")
            return {
                "total_users": 0,
                "by_account_type": {}
            }
    
    @staticmethod
    async def get_diagnostic_trends(days: int = 30) -> Dict[str, Any]:
        """
        Get diagnostic trends over time.
        
        Args:
            days: Number of days to analyze
            
        Returns:
            Dict with daily trend data
        """
        try:
            since_date = datetime.utcnow() - timedelta(days=days)
            
            query = f"""
                SELECT 
                    DATE(created_at) as date,
                    COUNT(*) as count,
                    AVG(score) as avg_score
                FROM diagnostics
                WHERE created_at >= '{since_date.isoformat()}'
                GROUP BY DATE(created_at)
                ORDER BY date ASC
            """
            
            result = await db.execute_query(query)
            
            trends = {
                "daily": []
            }
            
            if result:
                for row in result:
                    trends["daily"].append({
                        "date": str(row.get("date", "")),
                        "count": row.get("count", 0),
                        "avg_score": float(row.get("avg_score", 0)) if row.get("avg_score") else 0
                    })
            
            return trends
            
        except Exception as e:
            print(f"Error getting diagnostic trends: {e}")
            return {"daily": []}
    
    @staticmethod
    async def get_top_diagnostics(
        limit: int = 10,
        order_by: str = "score"  # score, views, recent
    ) -> List[Dict[str, Any]]:
        """
        Get top diagnostics by various metrics.
        
        Args:
            limit: Maximum number to return
            order_by: Sort by score, views, or recent
            
        Returns:
            List of diagnostic records
        """
        try:
            if order_by == "views":
                order_clause = "ORDER BY view_count DESC"
            elif order_by == "recent":
                order_clause = "ORDER BY created_at DESC"
            else:  # score
                order_clause = "ORDER BY score DESC"
            
            query = f"""
                SELECT 
                    id,
                    user_id,
                    score,
                    category,
                    view_count,
                    created_at
                FROM diagnostics
                {order_clause}
                LIMIT {limit}
            """
            
            result = await db.execute_query(query)
            
            diagnostics = []
            if result:
                for row in result:
                    diagnostics.append({
                        "id": row.get("id"),
                        "user_id": row.get("user_id"),
                        "score": row.get("score"),
                        "category": row.get("category"),
                        "view_count": row.get("view_count", 0),
                        "created_at": str(row.get("created_at", ""))
                    })
            
            return diagnostics
            
        except Exception as e:
            print(f"Error getting top diagnostics: {e}")
            return []
