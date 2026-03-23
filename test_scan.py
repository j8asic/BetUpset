import asyncio
from config import load_config
from main import initialize_platforms
from scanner import Scanner
from matching import group_matches_by_event

config = load_config("config.yaml")
platforms = initialize_platforms(config)
scanner = Scanner(platforms)
matches = scanner.scan()
print(f"Total matched groups: {len(matches)}")
