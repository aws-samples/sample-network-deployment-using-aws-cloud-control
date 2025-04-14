Here's an excerpt from your deploy_network_inspection function:

``` python

def deploy_network_inspection():
    """Deploy Network Inspection Resources."""
    config = load_yaml_config("network-firewall.yaml")

    for firewall in config["firewalls"]:
        # Multi-Region Support: Deploying in multiple regions as specified in the config
        account = firewall["account_id"]
        session = assume_role(account)
        cloudcontrol_client = session.client('cloudcontrol', region_name=firewall["region"])
        region = firewall["region"]

        # Dynamic Rule Configuration: Creating firewall policy based on config
        print(f"Deploying Firewall Policy: {firewall['firewall_policies']['name']} in {region}...")
        policy_properties = {
            "FirewallPolicyName": firewall["firewall_policies"]["name"],
            "FirewallPolicy": {
                "StatelessDefaultActions": firewall["firewall_policies"]["stateless_default_actions"],
                "StatelessFragmentDefaultActions": firewall["firewall_policies"]["stateless_fragment_default_actions"]
            },
            "Description": firewall["firewall_policies"]["description"]
        }
        # CRUD-L Operations: Create or fetch firewall policy (Fetch is happening from DynamoDB at the moment)
        policy_arn = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::NetworkFirewall::FirewallPolicy",
            firewall["firewall_policies"]["name"],
            account,
            region,
            properties=policy_properties
        )
        print(f"Firewall Policy deployed or fetched: {firewall['firewall_policies']['name']} -> {policy_arn}")

        # Fetch or create VPC and Subnet (omitted for brevity)

        # CRUD-L Operations: Create or fetch firewall
        print(f"Deploying Firewall: {firewall['name']} in {region}...")
        firewall_properties = {
            "FirewallName": firewall["name"],
            "FirewallPolicyArn": policy_arn,
            "VpcId": vpc_id,
            "SubnetMappings": [{"SubnetId": subnet_id}],
            "Tags": [{"Key": "Name", "Value": firewall["name"]}]
        }
        firewall_id = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::NetworkFirewall::Firewall",
            firewall["name"],
            account,
            region,
            properties=firewall_properties
        )
        print(f"Firewall deployed or fetched: {firewall['name']} -> {firewall_id}")

        # Fewer Manual Errors: Storing resource mapping in DynamoDB for consistency and easier management
        store_resource_mapping(
            resource_id=firewall_id,
            resource_name=firewall["name"],
            resource_type="AWS::NetworkFirewall::Firewall",
            account_id=account,
            region=region,
            metadata={
                "vpc_id": vpc_id,
                "subnet_id": subnet_id,
                "firewall_policy_arn": policy_arn
            }
        )
        print(f"Firewall stored in DynamoDB: {firewall['name']}")

# Simplified Traffic Routing: Configuring routes for inspection VPC
def inspection_vpc_routes():
    """Configure routes in Inspection VPC."""
    config = load_yaml_config("tgw-setup.yaml")

    for route_config in config["inspection_vpc_routes"]:
        session = assume_role(route_config["account_id"])
        cloudcontrol_client = session.client('cloudcontrol', region_name=route_config["region"])

        print(f"Configuring route: {route_config['destination_cidr_block']} in Route Table {route_config['route_table_name']}...")
        route_table_metadata = fetch_resource_by_name_and_type(
            route_config["route_table_name"], "AWS::EC2::RouteTable"
        )
        route_table_id = route_table_metadata["ResourceId"]

        if route_config["target_type"] == "FirewallEndpoint":
            # CRUD-L Operations: Read firewall details
            firewall_metadata = fetch_resource_by_name_and_type(
                route_config["target_name"], "AWS::NetworkFirewall::Firewall"
            )
            firewall_id = firewall_metadata["ResourceId"]
            firewall_details = get_resource(
                cloudcontrol_client, "AWS::NetworkFirewall::Firewall", firewall_id
            )
            endpoint_ids = firewall_details.get("EndpointIds", [])
            if not endpoint_ids:
                raise Exception(f"No Firewall Endpoints found for {route_config['target_name']}.")
            target_id = endpoint_ids[0].split(":")[1]  # Assumes only one endpoint is used
        elif route_config["target_type"] == "TGW":
            target_metadata = fetch_resource_by_name_and_type(
                route_config["target_name"], "AWS::EC2::TransitGateway"
            )
            target_id = target_metadata["ResourceId"]
        else:
            raise Exception(f"Unsupported target type: {route_config['target_type']}")

        # Dynamic Rule Configuration: Creating route based on config
        route_properties = {
            "RouteTableId": route_table_id,
            "DestinationCidrBlock": route_config["destination_cidr_block"]
        }

        if route_config["target_type"] == "FirewallEndpoint":
            route_properties["VpcEndpointId"] = target_id
        elif route_config["target_type"] == "TGW":
            route_properties["TransitGatewayId"] = target_id

        # CRUD-L Operations: Create or update route
        fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::EC2::Route",
            f"{route_config['route_table_name']}-{route_config['destination_cidr_block']}",
            route_config["account_id"],
            route_config["region"],
            properties=route_properties
        )
        print(f"Route configured: {route_config['destination_cidr_block']} -> {target_id}")
```


``` yaml
firewalls:
  - account_id: "Account-ID"
    region: "us-east-1"
    name: "CC-NetworkFirewall-East"
    vpc_name: "CC-Inspection-VPC-East"
    subnet_name: "CC-Inspection-Subnet-East"
    firewall_policies:
      name: "CC-FirewallPolicy-East"
      stateless_default_actions:
        - "aws:forward_to_sfe"
      stateless_fragment_default_actions:
        - "aws:forward_to_sfe"
      description: "Firewall policy for East region"

  - account_id: "Account-ID"
    region: "us-west-2"
    name: "CC-NetworkFirewall-West"
    vpc_name: "CC-Inspection-VPC-West"
    subnet_name: "CC-Inspection-Subnet-West"
    firewall_policies:
      name: "CC-FirewallPolicy-West"
      stateless_default_actions:
        - "aws:forward_to_sfe"
      stateless_fragment_default_actions:
        - "aws:forward_to_sfe"
      description: "Firewall policy for West region"
```

``` yaml
## Route table routes for the Inpsection VPC's subnet (TGW subnet and Inspection Subnet)
inspection_vpc_routes:
  - account_id: "Account-ID"
    region: "us-east-1"
    route_table_name: "CC-TGW-Subnet-East-RT"
    destination_cidr_block: "0.0.0.0/0"
    target_type: "FirewallEndpoint"
    target_name: "CC-NetworkFirewall-East"
  
  - account_id: "Account-ID"
    region: "us-east-1"
    route_table_name: "CC-Inspection-Subnet-East-RT"
    destination_cidr_block: "0.0.0.0/0"
    target_type: "TGW"
    target_name: "CC-CentralizedTGW-East"
  
  - account_id: "Account-ID"
    region: "us-west-2"
    route_table_name: "CC-TGW-Subnet-West-RT"
    destination_cidr_block: "0.0.0.0/0"
    target_type: "FirewallEndpoint"
    target_name: "CC-NetworkFirewall-West"
  
  - account_id: "Account-ID"
    region: "us-west-2"
    route_table_name: "CC-Inspection-Subnet-West-RT"
    destination_cidr_block: "0.0.0.0/0"
    target_type: "TGW"
    target_name: "CC-CentralizedTGW-West"

```
This code snippet highlights:

1. **Dynamic Rule Configuration:** Firewall policies and routes are created based on the configuration files.

2. **Multi-Region Support:** The script handles deployments across multiple regions as specified in the configuration.

3. **Simplified Traffic Routing:** The inspection_vpc_routes function configures routes for the inspection VPC, simplifying the process of setting up traffic flow through the firewall.

4. **CRUD-L Operations for Updates:** The fetch_or_create_resource function is used throughout to create or update resources. The get_resource function is used to read existing resource details.

5. **Fewer Manual Errors:** By using a declarative approach with configuration files and storing resource mappings in DynamoDB, the risk of manual errors is reduced. The consistent use of Cloud Control API also minimizes inconsistencies between different management tools.

These features collectively demonstrate how Cloud Control API simplifies the process of setting up and managing network inspection resources across multiple accounts and regions.