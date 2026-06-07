"""
ID generation utilities for diagnostics, snapshots, and blueprints.

Uses Python's secrets module for cryptographically secure random generation.
"""

import secrets
import string


def generate_diagnostic_id() -> str:
    """
    Generate unique diagnostic ID.
    
    Format: diag_{12 random alphanumeric characters}
    Example: diag_a7f3k9m2p5q1
    
    Returns:
        Unique diagnostic identifier
    """
    chars = string.ascii_lowercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(12))
    return f"diag_{random_part}"


def generate_snapshot_id() -> str:
    """
    Generate unique snapshot ID.
    
    Format: snap_{12 random alphanumeric characters}
    Example: snap_x4j8n2v7c9k3
    
    Returns:
        Unique snapshot identifier
    """
    chars = string.ascii_lowercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(12))
    return f"snap_{random_part}"


def generate_blueprint_id() -> str:
    """
    Generate unique blueprint ID.
    
    Format: bp_{12 random alphanumeric characters}
    Example: bp_m5t9r3w8h2n6
    
    Returns:
        Unique blueprint identifier
    """
    chars = string.ascii_lowercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(12))
    return f"bp_{random_part}"


def generate_id(prefix: str) -> str:
    """
    Generate unique ID with custom prefix.
    
    Format: {prefix}_{12 random alphanumeric characters}
    Example: user_a7f3k9m2p5q1
    
    Args:
        prefix: Prefix for the ID (e.g., 'user', 'session')
    
    Returns:
        Unique identifier with specified prefix
    """
    chars = string.ascii_lowercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(12))
    return f"{prefix}_{random_part}"
