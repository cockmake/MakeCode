"""Test script for RunGlob tool."""
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from utils.common import run_glob

# Colors for output
GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


def print_test(name: str, pattern: str, target_dir: str = ".", type: str = "all"):
    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}Test: {name}{RESET}")
    print(f"Pattern: {pattern}")
    print(f"Target:  {target_dir}")
    print(f"Type:    {type}")
    print(f"{CYAN}{'='*60}{RESET}")
    result = run_glob(pattern=pattern, target_dir=target_dir, type=type)
    print(result)
    return result


def main():
    print(f"\n{BOLD}{GREEN}RunGlob Test Suite{RESET}\n")
    
    # ========================================
    # File type tests (type="file")
    # ========================================
    print(f"\n{BOLD}{YELLOW}=== FILE TYPE TESTS ==={RESET}")
    
    # Test 1: Single pattern - Python files
    print_test("Single pattern: *.py", "*.py")
    
    # Test 2: Single pattern - JavaScript files
    print_test("Single pattern: *.js", "*.js")
    
    # Test 3: Multiple patterns with pipe
    print_test("Multiple patterns: *.py|*.js|*.ts", "*.py|*.js|*.ts")
    
    # Test 4: Recursive pattern
    print_test("Recursive: **/*.py", "**/*.py")
    
    # Test 5: Recursive with specific directory
    print_test("Recursive in utils: utils/**/*.py", "utils/**/*.py")
    
    # Test 6: Multiple recursive patterns
    print_test("Multiple recursive: utils/**/*.py|system/**/*.py", "utils/**/*.py|system/**/*.py")
    
    # Test 7: Specific target directory
    print_test("Target dir: utils", "*.py", "utils")
    
    # Test 8: Target dir with pattern
    print_test("Target dir + pattern: system", "*.py", "system")
    
    # Test 9: Config files
    print_test("Config files: *.json|*.yaml|*.yml|*.toml", "*.json|*.yaml|*.yml|*.toml")
    
    # Test 10: Test files pattern
    print_test("Test files: test_*.py|*_test.py", "test_*.py|*_test.py")
    
    # Test 11: No matches expected
    print_test("No matches: *.xyz", "*.xyz")
    
    # Test 12: All files (should exclude hidden and build dirs)
    print_test("All files: **/*", "**/*")
    
    # Test 13: Markdown files
    print_test("Markdown: *.md", "*.md")
    
    # Test 14: Multiple file types in one directory
    print_test("Mixed types in utils: utils", "*.py|*.json", "utils")
    
    # Test 15: Edge case - empty pattern part
    print_test("Edge case: *.py||*.js (empty part)", "*.py||*.js")
    
    # ========================================
    # Directory type tests (type="dir")
    # ========================================
    print(f"\n{BOLD}{YELLOW}=== DIRECTORY TYPE TESTS ==={RESET}")
    
    # Test 16: All directories in root
    print_test("All dirs in root", "*", type="dir")
    
    # Test 17: Directories matching pattern
    print_test("Dirs matching 's*'", "s*", type="dir")
    
    # Test 18: Recursive directories
    print_test("All dirs recursively", "**/*", type="dir")
    
    # Test 19: Directories in specific target
    print_test("Dirs in utils", "*", "utils", type="dir")
    
    # Test 20: Directories matching pattern in target
    print_test("Dirs matching 'c*' in utils", "c*", "utils", type="dir")
    
    # ========================================
    # All type tests (type="all")
    # ========================================
    print(f"\n{BOLD}{YELLOW}=== ALL TYPE TESTS ==={RESET}")
    
    # Test 21: All items in root
    print_test("All items in root", "*", type="all")
    
    # Test 22: All items recursively
    print_test("All items recursively", "**/*", type="all")
    
    # Test 23: All items matching pattern
    print_test("All .py files and dirs matching s*", "*.py|s*", type="all")
    
    # Test 24: All items in specific dir
    print_test("All items in utils", "*", "utils", type="all")
    
    # ========================================
    # Error handling tests
    # ========================================
    print(f"\n{BOLD}{YELLOW}=== ERROR HANDLING TESTS ==={RESET}")
    
    # Test 25: Invalid type
    print_test("Invalid type", "*.py", type="invalid")
    
    # Test 26: Non-existent directory
    print_test("Non-existent dir", "*.py", "nonexistent")
    
    print(f"\n{BOLD}{GREEN}Tests completed!{RESET}\n")


if __name__ == "__main__":
    main()
