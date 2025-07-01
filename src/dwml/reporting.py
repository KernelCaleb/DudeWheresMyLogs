import csv
from datetime import datetime

def print_banner():
    """Print a banner for the script"""
    print("""
=========================================================================
_______   __    __   _______   _______                                  
|       \ |  |  |  | |       \ |   ____|                                 
|  .--.  ||  |  |  | |  .--.  ||  |__                                    
|  |  |  ||  |  |  | |  |  |  ||   __|                                   
|  '--'  ||  `--'  | |  '--'  ||  |____                                  
|_______/  \______/  |_______/ |_______|                                 
                                                                         
====    __    ____  __    __   _______ .______       _______     _______.
\   \  /  \  /   / |  |  |  | |   ____||   _  \     |   ____|   /       |
 \   \/    \/   /  |  |__|  | |  |__   |  |_)  |    |  |__     |   (----`
  \            /   |   __   | |   __|  |      /     |   __|     \   \    
   \    /\    /    |  |  |  | |  |____ |  |\  \----.|  |____.----)   |   
    \__/  \__/     |__|  |__| |_______|| _| `._____||_______|_______/    
                                                                         
.___  ___. ____    ____                                                  
|   \/   | \   \  /   /                                                  
|  \  /  |  \   \/   /                                                   
|  |\/|  |   \_    _/                                                    
|  |  |  |     |  |                                                      
|__|  |__|     |__|                                                      
                                                                         
 __        ______     _______      _______.______                        
|  |      /  __  \   /  _____|    /       |      \                       
|  |     |  |  |  | |  |  __     |   (----`----)  |                      
|  |     |  |  |  | |  | |_ |     \   \       /  /                       
|  `----.|  `--'  | |  |__| | .----)   |     |__|                        
|_______| \______/   \______| |_______/       __                         
                                             (__)    
=========================================================================
    """)

def print_diagnostic_report(diagnostic_data):
    """Print a report of diagnostic settings"""
    log_destinations = diagnostic_data["log_destinations"]
    duplicate_logging = diagnostic_data["duplicate_logging"]
    
    print("\n=== Diagnostic Settings Report ===\n")
    
    # Highlight resources with duplicate logging
    if duplicate_logging:
        print("\n DUPLICATE LOGGING DETECTED ")
        print("The following resources have multiple diagnostic settings destinations:")
        for item in duplicate_logging:
            resource = item["resource"]
            
            print(f"  - Resource: {resource['name']} (Type: {resource['type']})")
            
            # Handle standard duplicate logging
            if 'destinations' in item:
                # Count occurrences of each destination
                dest_counts = {}
                for dest in item['destinations']:
                    dest_counts[dest] = dest_counts.get(dest, 0) + 1
                
                print("    Duplicate Destinations:")
                for dest, count in dest_counts.items():
                    if count > 1:
                        print(f"    * {dest}: {count} times")
                
                print("    Full Destinations:")
                for detail in item.get('destination_details', []):
                    print(f"    * {detail['type']}: {detail['id']}")
            
            # Handle storage account specific diagnostics
            if 'storage_diagnostics' in item:
                print(f"\nStorage Account Diagnostic Details for {resource['name']}:")
                for service_diag in item['storage_diagnostics']:
                    print(f"  Service: {service_diag['service']}")
                    for destinations in service_diag['destinations']:
                        print("    Destinations:")
                        for dest in destinations:
                            print(f"      - Type: {dest['type']}, ID: {dest['id']}")
            
            print(f"    Resource ID: {resource['id']}\n")
    else:
        print("\n No resources with duplicate logging configurations found.")
    
    # Resources without diagnostic settings
    print(f"Resources without diagnostic settings: {len(log_destinations['none'])}")
    for resource in log_destinations['none'][:5]:  # Show first 5
        print(f"  - {resource['name']} ({resource['type']})")
    if len(log_destinations['none']) > 5:
        print(f"  ... and {len(log_destinations['none']) - 5} more")
    
    # Resources with logs going to different destinations
    print(f"\nResources sending logs to Log Analytics: {len(log_destinations['log_analytics'])}")
    print(f"Resources sending logs to Storage Account: {len(log_destinations['storage_account'])}")
    print(f"Resources sending logs to Event Hub: {len(log_destinations['event_hub'])}")
    print(f"Resources sending logs to Partner Solutions: {len(log_destinations['partner_solution'])}")

def export_to_csv(csv_data, subscription_name):
    """Export data to CSV file"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"DudeWheresMyLogs_{subscription_name.replace(' ', '_')}_{timestamp}.csv"
    
    print(f"\nExporting results to {filename}...")
    
    with open(filename, 'w', newline='') as csvfile:
        fieldnames = ["Resource Type", "Resource Name", "Entity", "Diagnostic Enabled", "Diagnostic Log Destination"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        
        writer.writeheader()
        for row in csv_data:
            writer.writerow(row)
    
    print(f"CSV export complete: {filename}")
    return filename
