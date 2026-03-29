"""
bob.py - API Utility Module

A collection of utility functions for API development.

Author: bob
Created: 2026-03-29
"""

from typing import Optional, Dict, Any
from datetime import datetime


def greet(name: str = "World") -> str:
    """
    Generate a greeting message.
    
    Args:
        name: The name to greet. Defaults to "World".
    
    Returns:
        A greeting string.
    
    Examples:
        >>> greet("Alice")
        'Hello, Alice!'
        >>> greet()
        'Hello, World!'
    """
    return f"Hello, {name}!"


def process_request(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process an incoming API request and return a standardized response.
    
    Args:
        data: The request data dictionary.
    
    Returns:
        A response dictionary with status, timestamp, and processed data.
    """
    return {
        "status": "success",
        "timestamp": datetime.utcnow().isoformat(),
        "data": data,
        "message": "Request processed successfully"
    }


def validate_input(value: Any, expected_type: type) -> bool:
    """
    Validate that a value matches the expected type.
    
    Args:
        value: The value to validate.
        expected_type: The expected type (e.g., str, int, list).
    
    Returns:
        True if the value matches the expected type, False otherwise.
    """
    return isinstance(value, expected_type)


def format_response(success: bool, message: str, data: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Format a standardized API response.
    
    Args:
        success: Whether the operation was successful.
        message: A descriptive message.
        data: Optional data payload.
    
    Returns:
        A formatted response dictionary.
    """
    response = {
        "success": success,
        "message": message,
        "timestamp": datetime.utcnow().isoformat()
    }
    if data is not None:
        response["data"] = data
    return response


if __name__ == "__main__":
    # Example usage and tests
    print("=== bob.py API Module ===\n")
    
    # Test greet function
    print("1. Greeting examples:")
    print(f"   {greet()}")
    print(f"   {greet('Developer')}\n")
    
    # Test process_request function
    print("2. Request processing:")
    sample_request = {"action": "test", "value": 42}
    result = process_request(sample_request)
    print(f"   Input: {sample_request}")
    print(f"   Output: {result}\n")
    
    # Test validate_input function
    print("3. Input validation:")
    print(f"   Is 'hello' a string? {validate_input('hello', str)}")
    print(f"   Is 42 a string? {validate_input(42, str)}")
    print(f"   Is 42 an int? {validate_input(42, int)}\n")
    
    # Test format_response function
    print("4. Response formatting:")
    resp = format_response(True, "Operation completed", {"result": "ok"})
    print(f"   Success response: {resp}\n")
    
    print("=== All examples completed ===")
