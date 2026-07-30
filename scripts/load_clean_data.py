import json
import os
import sys

def main():
    input_file = "all_data.json"
    output_file = "all_data_clean.json"

    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found.")
        sys.exit(1)

    print(f"Reading {input_file}...")
    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("Filtering out contenttypes and auth permissions...")
    clean_data = []
    excluded_models = {"contenttypes.contenttype", "auth.permission"}
    
    for item in data:
        if item.get("model") not in excluded_models:
            clean_data.append(item)

    print(f"Writing cleaned data to {output_file}...")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(clean_data, f, indent=2)

    print("Done! You can now run: python manage.py loaddata all_data_clean.json")

if __name__ == "__main__":
    main()
