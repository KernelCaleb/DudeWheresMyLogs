import subprocess
import json
import sys

def run_az_command(command):
    """Run Azure CLI command and return JSON output"""
    try:
        result = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error executing Azure command: {e}")
        print(f"Error details: {e.stderr.decode()}")
        sys.exit(1)

def check_az_cli():
    """Check if Azure CLI is installed and logged in"""
    try:
        subprocess.run(["az", "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Azure CLI not found. Please install it first: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli")
        sys.exit(1)
    
    # Check if logged in
    try:
        subprocess.run(["az", "account", "show"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("Already logged in to Azure.")
    except subprocess.CalledProcessError:
        print("Please login to Azure first:")
        subprocess.run(["az", "login"], check=True)

def list_subscriptions():
    """List available subscriptions"""
    subscriptions = run_az_command("az account list")
    print("\nAvailable Subscriptions:")
    for i, sub in enumerate(subscriptions):
        print(f"[{i}] {sub['name']} ({sub['id']})")
    
    print("\nSelection Options:")
    print("[A] Scan ALL Subscriptions")
    print("[Q] Quit")
    
    while True:
        choice = input("\nEnter your choice (number, A for all, or Q to quit): ").strip().upper()
        
        if choice == 'Q':
            sys.exit(0)
        
        if choice == 'A':
            return subscriptions
        
        try:
            choice = int(choice)
            if 0 <= choice < len(subscriptions):
                return [subscriptions[choice]]
            print("Invalid choice. Please try again.")
        except ValueError:
            print("Please enter a number, A, or Q.")

def enumerate_resources(subscription_id):
    """Enumerate all resources in a subscription"""
    print(f"\nEnumerating all resources in subscription: {subscription_id}...")
    resources = run_az_command(f"az resource list --subscription {subscription_id}")
    print(f"Found {len(resources)} resources.")
    
    return resources
