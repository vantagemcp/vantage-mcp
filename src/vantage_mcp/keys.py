"""Manual API key issuance - no signup page yet, so this is the pilot's
key-issuance path. Run: python -m vantage_mcp.keys create --tier=free
"""

import argparse

from vantage_mcp import store


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create")
    create.add_argument("--tier", default="free", choices=list(store.TIER_LIMITS))
    args = parser.parse_args()

    if args.cmd == "create":
        plaintext, client_id = store.create_api_key(tier=args.tier)
        print(f"client_id: {client_id}")
        print(f"tier:      {args.tier} ({store.TIER_LIMITS[args.tier]} calls/month)")
        print(f"api_key:   {plaintext}")
        print("\nShown once - store it now. Use as a Bearer token against the hosted endpoint.")


if __name__ == "__main__":
    main()
