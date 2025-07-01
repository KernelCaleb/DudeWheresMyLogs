from .azure import check_az_cli, list_subscriptions, enumerate_resources
from .diagnostics import check_diagnostic_settings
from .reporting import print_banner, print_diagnostic_report, export_to_csv

def main():
    print("DudeWheresMyLogs - Azure Diagnostic Log Health Checker")
    print_banner()
    check_az_cli()
    
    # Get subscriptions to scan
    subscriptions = list_subscriptions()
    
    # Consolidated results
    consolidated_diagnostic_data = {
        "log_destinations": {
            "log_analytics": [],
            "storage_account": [],
            "event_hub": [],
            "partner_solution": [],
            "none": []
        },
        "duplicate_logging": [],
        "csv_data": []
    }
    
    # Scan each subscription
    for subscription in subscriptions:
        print(f"\nScanning subscription: {subscription['name']} ({subscription['id']})")
        
        # Enumerate resources
        resources = enumerate_resources(subscription['id'])
        
        # Check diagnostic settings
        diagnostic_data = check_diagnostic_settings(subscription['id'], resources)
        
        # Consolidate results
        for key in consolidated_diagnostic_data["log_destinations"]:
            consolidated_diagnostic_data["log_destinations"][key].extend(
                diagnostic_data["log_destinations"][key]
            )
        
        consolidated_diagnostic_data["duplicate_logging"].extend(
            diagnostic_data["duplicate_logging"]
        )
        
        consolidated_diagnostic_data["csv_data"].extend(
            diagnostic_data["csv_data"]
        )
    
    # Print diagnostic report
    print_diagnostic_report(consolidated_diagnostic_data)
    
    # Export to CSV
    csv_filename = export_to_csv(
        consolidated_diagnostic_data["csv_data"], 
        "MultiSubscription" if len(subscriptions) > 1 else subscriptions[0]['name']
    )
    
    # Print completion message
    print(f"\nDudeWheresMyLogs scan complete! 🎉")
    print(f"Results saved to: {csv_filename}")

if __name__ == "__main__":
    main()
