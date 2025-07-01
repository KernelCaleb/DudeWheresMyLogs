from .azure import run_az_command

def supports_diagnostic_settings(resource_type):
    """Check if a resource type supports diagnostic settings"""
    # List of resource types that support diagnostic settings
    supported_types = [
        "microsoft.compute/virtualmachines",
        "microsoft.storage/storageaccounts",
        "microsoft.network/networkinterfaces",
        "microsoft.network/networksecuritygroups",
        "microsoft.network/applicationgateways",
        "microsoft.sql/servers/databases",
        "microsoft.keyvault/vaults",
        "microsoft.web/sites",
        # Add more as needed
    ]
    
    return resource_type.lower() in supported_types

def check_storage_account_diagnostics(resource_id):
    """
    Check diagnostic settings for different storage account services
    
    Services to check:
    - Storage Account (overall)
    - Blob Service
    - Queue Service
    - Table Service
    - File Service
    """
    services = [
        {"name": "Storage Account", "command": f"az monitor diagnostic-settings list --resource {resource_id}"},
        {"name": "Blob Service", "command": f"az monitor diagnostic-settings list --resource {resource_id}/blobServices/default"},
        {"name": "Queue Service", "command": f"az monitor diagnostic-settings list --resource {resource_id}/queueServices/default"},
        {"name": "Table Service", "command": f"az monitor diagnostic-settings list --resource {resource_id}/tableServices/default"},
        {"name": "File Service", "command": f"az monitor diagnostic-settings list --resource {resource_id}/fileServices/default"}
    ]
    
    detailed_diagnostics = []
    
    for service in services:
        try:
            diag_settings = run_az_command(service["command"])
            
            if diag_settings:
                service_destinations = []
                for setting in diag_settings:
                    destinations = []
                    
                    if setting.get('workspaceId'):
                        destinations.append({
                            "type": "Log Analytics",
                            "id": setting.get('workspaceId')
                        })
                    
                    if setting.get('storageAccountId'):
                        destinations.append({
                            "type": "Storage Account",
                            "id": setting.get('storageAccountId')
                        })
                    
                    if setting.get('eventHubAuthorizationRuleId') or setting.get('eventHubName'):
                        destinations.append({
                            "type": "Event Hub",
                            "id": setting.get('eventHubAuthorizationRuleId') or setting.get('eventHubName')
                        })
                    
                    if setting.get('marketplacePartnerId'):
                        destinations.append({
                            "type": "Partner Solution",
                            "id": setting.get('marketplacePartnerId')
                        })
                    
                    service_destinations.append(destinations)
                
                detailed_diagnostics.append({
                    "service": service["name"],
                    "destinations": service_destinations
                })
        
        except Exception as e:
            print(f"Error checking {service['name']} diagnostic settings: {e}")
    
    return detailed_diagnostics

def check_diagnostic_settings(subscription_id, resources):
    """Check diagnostic settings for each resource and prepare data for CSV"""
    print("\nChecking diagnostic settings...")
    
    # List to store data for CSV export
    csv_data = []
    
    # Dictionary to track where logs are going
    log_destinations = {
        "log_analytics": [],
        "storage_account": [],
        "event_hub": [],
        "partner_solution": [],
        "none": []
    }
    
    # Resources with duplicate logging
    duplicate_logging = []
    
    # Track progress
    total = len(resources)
    processed = 0
    
    for resource in resources:
        processed += 1
        if processed % 10 == 0:
            print(f"Progress: {processed}/{total} resources checked")
        
        resource_id = resource['id']
        resource_name = resource['name']
        resource_type = resource['type']
        
        # Skip resource types that don't support diagnostic settings
        if not supports_diagnostic_settings(resource_type):
            # Add to CSV data with N/A for diagnostic settings
            csv_data.append({
                "Resource Type": resource_type,
                "Resource Name": resource_name,
                "Entity": resource_id,
                "Diagnostic Enabled": "N/A",
                "Diagnostic Log Destination": "N/A"
            })
            continue
            
        # Get diagnostic settings for this resource
        try:
            diag_settings = run_az_command(f"az monitor diagnostic-settings list --resource {resource_id}")
            
            if not diag_settings:
                log_destinations["none"].append(resource)
                
                # Add to CSV data
                csv_data.append({
                    "Resource Type": resource_type,
                    "Resource Name": resource_name,
                    "Entity": resource_id,
                    "Diagnostic Enabled": "No",
                    "Diagnostic Log Destination": "None"
                })
                continue
                
            # Check where logs are going
            destinations = []
            destination_details = []
            for setting in diag_settings:
                if setting.get('workspaceId'):
                    destinations.append("Log Analytics")
                    destination_details.append({
                        "type": "Log Analytics",
                        "id": setting.get('workspaceId')
                    })
                    log_destinations["log_analytics"].append(resource)
                    
                if setting.get('storageAccountId'):
                    destinations.append("Storage Account")
                    destination_details.append({
                        "type": "Storage Account", 
                        "id": setting.get('storageAccountId')
                    })
                    log_destinations["storage_account"].append(resource)
                    
                if setting.get('eventHubAuthorizationRuleId') or setting.get('eventHubName'):
                    destinations.append("Event Hub")
                    destination_details.append({
                        "type": "Event Hub",
                        "id": setting.get('eventHubAuthorizationRuleId') or setting.get('eventHubName')
                    })
                    log_destinations["event_hub"].append(resource)
                    
                if setting.get('marketplacePartnerId'):
                    destinations.append("Partner Solution")
                    destination_details.append({
                        "type": "Partner Solution",
                        "id": setting.get('marketplacePartnerId')
                    })
                    log_destinations["partner_solution"].append(resource)
            
            # Add to CSV data
            csv_data.append({
                "Resource Type": resource_type,
                "Resource Name": resource_name,
                "Entity": resource_id,
                "Diagnostic Enabled": "Yes",
                "Diagnostic Log Destination": ", ".join(set(destinations)) if destinations else "None"
            })
            
            # Check for duplicate logging
            if len(destinations) > len(set(destinations)):
                duplicate_logging.append({
                    "resource": resource,
                    "destinations": destinations,
                    "destination_details": destination_details
                })
            
            # Special handling for storage accounts
            if resource_type.lower() == 'microsoft.storage/storageaccounts':
                storage_diagnostics = check_storage_account_diagnostics(resource_id)
                
                # Check for duplicate logging across services
                if storage_diagnostics:
                    all_destinations = [dest for service in storage_diagnostics for destinations in service['destinations'] for dest in destinations]
                    
                    if len(all_destinations) > len(set(tuple(d.items()) for d in all_destinations)):
                        duplicate_logging.append({
                            "resource": resource,
                            "storage_diagnostics": storage_diagnostics
                        })
                
        except Exception as e:
            print(f"Error checking diagnostic settings for {resource_name}: {e}")
            # Add error entry to CSV
            csv_data.append({
                "Resource Type": resource_type,
                "Resource Name": resource_name,
                "Entity": resource_id,
                "Diagnostic Enabled": "Error",
                "Diagnostic Log Destination": f"Error: {str(e)[:100]}"
            })
    
    return {
        "log_destinations": log_destinations,
        "duplicate_logging": duplicate_logging,
        "csv_data": csv_data
    }
