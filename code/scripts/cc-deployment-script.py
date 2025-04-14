import boto3
import yaml
import json
import time
import os


DEPLOYMENT_REGION = "us-east-1"
DYNAMODB_TABLE_NAME = "CloudControlResourceState"
GSI_INDEX_NAME = "ResourceNameIndex"

# Initialize the DynamoDB client for the deployment account and region
dynamodb_client = boto3.client("dynamodb", region_name=DEPLOYMENT_REGION)


# Function to assume role into the target account
def assume_role(account_id, role_name="MemberAccountDeploymentRole"):
    """Assume role in the target account and return a boto3 session."""
    sts_client = boto3.client('sts')
    assumed_role = sts_client.assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/{role_name}",
        RoleSessionName="AssumeRoleSession"
    )
    credentials = assumed_role['Credentials']
    return boto3.Session(
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )


# Load YAML configuration file
def load_yaml_config(file_name):
    """Load YAML configuration file."""
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_name)
    with open(file_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)


# Wait for resource creation/update to complete
def wait_for_resource(cloudcontrol_client, request_token):
    """Wait for the resource operation to complete."""
    while True:
        response = cloudcontrol_client.get_resource_request_status(RequestToken=request_token)
        if response['ProgressEvent']['OperationStatus'] in ['SUCCESS', 'FAILED']:
            return response
        time.sleep(2)


# Store resource mapping in DynamoDB
def store_resource_mapping(resource_id, resource_name, resource_type, account_id, region, metadata=None):
    """Store resource mapping in DynamoDB."""
    try:
        dynamodb_client.put_item(
            TableName=DYNAMODB_TABLE_NAME,
            Item={
                'ResourceId': {'S': resource_id},
                'ResourceName': {'S': resource_name},
                'ResourceType': {'S': resource_type},
                'AccountId': {'S': account_id},
                'Region': {'S': region},
                'AdditionalMetadata': {'S': json.dumps(metadata) if metadata else '{}'}
            }
        )
        print(f"Resource mapping stored in DynamoDB: {resource_name} -> {resource_id}")
    except Exception as e:
        print(f"Failed to store resource mapping in DynamoDB: {e}")
        raise


# Fetch resource details from DynamoDB
def fetch_resource_by_name_and_type(resource_name, resource_type):
    """Fetch resource details from DynamoDB using ResourceName and ResourceType via GSI."""
    try:
        response = dynamodb_client.query(
            TableName=DYNAMODB_TABLE_NAME,
            IndexName=GSI_INDEX_NAME,
            KeyConditionExpression='ResourceName = :resource_name AND ResourceType = :resource_type',
            ExpressionAttributeValues={
                ':resource_name': {'S': resource_name},
                ':resource_type': {'S': resource_type}
            }
        )
        if 'Items' in response and response['Items']:
            item = response['Items'][0]
            return {
                'ResourceId': item['ResourceId']['S'],
                'AccountId': item['AccountId']['S'],
                'Region': item['Region']['S'],
                'AdditionalMetadata': json.loads(item['AdditionalMetadata']['S'])
            }
        print(f"Resource with name {resource_name} and type {resource_type} not found in DynamoDB.")
        return None
    except Exception as e:
        print(f"Unexpected error querying DynamoDB: {e}")
        return None


# # Fetch or create a resource
def fetch_or_create_resource(cloudcontrol_client, resource_type, resource_name, account_id, region, properties=None):
    """
    Fetch resource ID dynamically using DynamoDB and Cloud Control API.
    If not found, create the resource and store it in DynamoDB.
    """
    # Check for resource in DynamoDB
    resource = fetch_resource_by_name_and_type(resource_name, resource_type)
    if resource:
        print(f"Found resource in DynamoDB: {resource_name} -> {resource['ResourceId']}")
        return resource['ResourceId']

    if not properties:
        raise Exception(f"Properties required to create resource {resource_name} of type {resource_type}.")

    print(f"Creating resource: {resource_name} of type {resource_type}...")
    response = cloudcontrol_client.create_resource(
        TypeName=resource_type,
        DesiredState=json.dumps(properties)
    )
    request_token = response['ProgressEvent']['RequestToken']

    # Loop to wait for resource operation completion
    while True:
        result = cloudcontrol_client.get_resource_request_status(RequestToken=request_token)
        operation_status = result['ProgressEvent']['OperationStatus']

        if operation_status == 'SUCCESS':
            resource_id = result['ProgressEvent']['Identifier']
            print(f"Resource created successfully: {resource_name} -> {resource_id}")
            store_resource_mapping(
                resource_id,
                resource_name,
                resource_type,
                account_id,
                region,
                metadata=properties
            )
            return resource_id
        elif operation_status == 'FAILED':
            raise Exception(f"Failed to create resource {resource_name}: {result['ProgressEvent']['StatusMessage']}")
        elif operation_status == 'IN_PROGRESS':
            # Handle specific case for Transit Gateway Attachment
            if resource_type == "AWS::EC2::TransitGatewayAttachment":
                attachment_id = result.get('ProgressEvent', {}).get('Identifier')
                if attachment_id:
                    print(f"TGW Attachment {attachment_id} is in 'pendingAcceptance' state. Proceeding for acceptance.")
                    # Store the resource for tracking
                    store_resource_mapping(
                        resource_id=attachment_id,
                        resource_name=resource_name,
                        resource_type=resource_type,
                        account_id=account_id,
                        region=region,
                        metadata=properties
                    )
                    return attachment_id
            print(f"Resource creation in progress: {resource_name}. Retrying in 5 seconds...")
            time.sleep(5)
        else:
            raise Exception(f"Unexpected operation status: {operation_status}")


def get_resource(cloudcontrol_client, type_name, identifier):
    """
    Fetch a resource's properties using the AWS Cloud Control API.

    Args:
        cloudcontrol_client: Boto3 Cloud Control client.
        type_name (str): The resource type (e.g., "AWS::NetworkFirewall::Firewall").
        identifier (str): The resource's unique identifier or ARN.

    Returns:
        dict: The properties of the requested resource as a dictionary.
    
    Raises:
        Exception: If the resource is not found or an error occurs.
    """
    try:
        print(f"Fetching resource: {type_name} with identifier: {identifier}")
        response = cloudcontrol_client.get_resource(
            TypeName=type_name,
            Identifier=identifier,
        )
        properties = json.loads(response["ResourceDescription"]["Properties"])
        print(f"Resource fetched successfully: {properties}")
        return properties
    except cloudcontrol_client.exceptions.ResourceNotFoundException:
        print(f"Resource not found: {type_name} with identifier: {identifier}")
        return None
    except Exception as e:
        print(f"Failed to fetch resource {type_name} with identifier {identifier}: {e}")
        raise


def create_and_accept_tgw_attachment(attachment_name, tgw_name, vpc_id, subnet_ids, member_account_id, region):
    """Creates a TGW attachment in the member account and handles acceptance from the central account."""
    try:
        # Step 1: Create TGW Attachment using Cloud Control API
        print(f"Creating TGW Attachment {attachment_name} in member account {member_account_id}...")
        session = assume_role(member_account_id)
        cloudcontrol_client = session.client('cloudcontrol', region_name=region)

        tgw_metadata = fetch_resource_by_name_and_type(
            tgw_name, "AWS::EC2::TransitGateway")
        tgw_id = tgw_metadata["ResourceId"]
        tgw_account_id = tgw_metadata["AccountId"]
        tgw_region = tgw_metadata["Region"]

        attachment_properties = {
            "TransitGatewayId": tgw_id,
            "VpcId": vpc_id,
            "SubnetIds": subnet_ids,
            "Tags": [{"Key": "Name", "Value": attachment_name}]
        }

        tgw_attachment_id = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::EC2::TransitGatewayAttachment",
            attachment_name,
            member_account_id,
            region,
            properties=attachment_properties
        )
        print(f"TGW Attachment created: {tgw_attachment_id}")

        # Step 2: Check the state of the TGW attachment
        print("Checking the state of the TGW Attachment...")
        ec2_client = session.client("ec2", region_name=region)
        response = ec2_client.describe_transit_gateway_vpc_attachments(
            TransitGatewayAttachmentIds=[tgw_attachment_id]
        )
        attachment_state = response["TransitGatewayVpcAttachments"][0]["State"]

        if attachment_state == "available":
            print(f"TGW Attachment {tgw_attachment_id} is already in 'available' state. Skipping acceptance.")
        elif attachment_state == "pendingAcceptance":
            print(f"TGW Attachment {tgw_attachment_id} is in 'pendingAcceptance' state. Proceeding with acceptance.")
            # Accept TGW Attachment in the central account
            central_session = assume_role(tgw_account_id)
            ec2_client_central = central_session.client('ec2', region_name=tgw_region)
            print(f"Accepting TGW Attachment {tgw_attachment_id} in central account {tgw_account_id}...")
            ec2_client_central.accept_transit_gateway_vpc_attachment(
                TransitGatewayAttachmentId=tgw_attachment_id
            )
            print(f"TGW Attachment {tgw_attachment_id} accepted successfully.")
        elif attachment_state in ["failed", "deleted"]:
            raise Exception(f"Attachment creation failed with state: {attachment_state}")
        else:
            print(f"TGW Attachment {tgw_attachment_id} is in unexpected state: {attachment_state}. Exiting.")

        # Step 3: Add a name tag for easier identification
        print(f"Tagging TGW Attachment {tgw_attachment_id} in central account...")
        central_session = assume_role(tgw_account_id)
        ec2_client_central = central_session.client('ec2', region_name=tgw_region)
        ec2_client_central.create_tags(
            Resources=[tgw_attachment_id],
            Tags=[{"Key": "Name", "Value": attachment_name}]
        )

        # Step 4: Store TGW Attachment in DynamoDB
        store_resource_mapping(
            resource_id=tgw_attachment_id,
            resource_name=attachment_name,
            resource_type="AWS::EC2::TransitGatewayAttachment",
            account_id=member_account_id,
            region=region,
            metadata={"tgw_id": tgw_id, "vpc_id": vpc_id, "subnet_ids": subnet_ids}
        )
        print(f"TGW Attachment {attachment_name} stored in DynamoDB successfully.")

        return tgw_attachment_id

    except Exception as e:
        print(f"Failed to create or accept TGW Attachment {attachment_name}: {e}")
        raise


# def create_tgw_attachment_route_tables():
#     """Create TGW Route Tables and configure routes."""
#     config = load_yaml_config("tgw-setup.yaml")

#     for route_table_config in config["tgw_attachment_route_tables"]:
#         session = assume_role(route_table_config["account_id"])
#         cloudcontrol_client = session.client('cloudcontrol', region_name=route_table_config["region"])

#         # Create TGW Route Table
#         print(f"Creating TGW Route Table: {route_table_config['route_table_name']}...")
#         tgw_metadata = fetch_resource_by_name_and_type(
#             route_table_config["tgw_name"], "AWS::EC2::TransitGateway"
#         )
#         tgw_id = tgw_metadata["ResourceId"]

#         route_table_properties = {
#             "TransitGatewayId": tgw_id,
#             "Tags": [{"Key": "Name", "Value": route_table_config["route_table_name"]}]
#         }
#         tgw_route_table_id = fetch_or_create_resource(
#             cloudcontrol_client,
#             "AWS::EC2::TransitGatewayRouteTable",
#             route_table_config["route_table_name"],
#             route_table_config["account_id"],
#             route_table_config["region"],
#             properties=route_table_properties
#         )
#         print(f"TGW Route Table created: {route_table_config['route_table_name']} -> {tgw_route_table_id}")

#         # Store TGW Route Table in DynamoDB
#         store_resource_mapping(
#             resource_id=tgw_route_table_id,
#             resource_name=route_table_config["route_table_name"],
#             resource_type="AWS::EC2::TransitGatewayRouteTable",
#             account_id=route_table_config["account_id"],
#             region=route_table_config["region"],
#             metadata={"tgw_id": tgw_id}
#         )

#         # Add Static Routes to the Route Table
#         print(f"Configuring routes for TGW Route Table {route_table_config['route_table_name']}...")
#         for route in route_table_config["routes"]:
#             target_attachment = fetch_resource_by_name_and_type(
#                 route["target_attachment_name"], "AWS::EC2::TransitGatewayAttachment"
#             )
#             if not target_attachment:
#                 raise Exception(f"TGW Attachment {route['target_attachment_name']} not found.")

#             route_properties = {
#                 "TransitGatewayRouteTableId": tgw_route_table_id,
#                 "DestinationCidrBlock": route["destination_cidr_block"],
#                 "TransitGatewayAttachmentId": target_attachment["ResourceId"]
#             }
#             fetch_or_create_resource(
#                 cloudcontrol_client,
#                 "AWS::EC2::TransitGatewayRoute",
#                 f"{route_table_config['route_table_name']}-{route['destination_cidr_block']}",
#                 route_table_config["account_id"],
#                 route_table_config["region"],
#                 properties=route_properties
#             )
#             print(f"Route added: {route['destination_cidr_block']} -> {target_attachment['ResourceId']}")


# Disassociate TGW Route Table
def disassociate_tgw_route_table(ec2_client, attachment_id):
    try:
        response = ec2_client.describe_transit_gateway_attachments(
            TransitGatewayAttachmentIds=[attachment_id]
        )
        attachment = response["TransitGatewayAttachments"][0]
        current_association = attachment.get("Association", {})
        current_route_table_id = current_association.get("TransitGatewayRouteTableId")

        if current_route_table_id:
            print(f"Disassociating Attachment {attachment_id} from Route Table {current_route_table_id}...")
            ec2_client.disassociate_transit_gateway_route_table(
                TransitGatewayRouteTableId=current_route_table_id,
                TransitGatewayAttachmentId=attachment_id
            )
            print(f"Disassociated Attachment {attachment_id} from {current_route_table_id}.")
        else:
            print(f"Attachment {attachment_id} is not associated with any Route Table.")
    except Exception as e:
        print(f"Failed to disassociate TGW Route Table for Attachment {attachment_id}: {e}")
        raise


# Add Inspection VPC Routes
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
            # Use Cloud Control API to fetch Firewall details
            firewall_metadata = fetch_resource_by_name_and_type(
                route_config["target_name"], "AWS::NetworkFirewall::Firewall"
            )
            firewall_id = firewall_metadata["ResourceId"]
            firewall_details = get_resource(
                cloudcontrol_client, "AWS::NetworkFirewall::Firewall", firewall_id
            )
            #firewall_properties = json.loads(firewall_details)
            endpoint_ids = firewall_details.get("EndpointIds", [])
            if not endpoint_ids:
                raise Exception(f"No Firewall Endpoints found for {route_config['target_name']}.")
            # Extract the VPC endpoint ID (remove AZ prefix)
            target_id = endpoint_ids[0].split(":")[1]  # Assumes only one endpoint is used
        elif route_config["target_type"] == "TGW":
            target_metadata = fetch_resource_by_name_and_type(
                route_config["target_name"], "AWS::EC2::TransitGateway"
            )
            target_id = target_metadata["ResourceId"]
        else:
            raise Exception(f"Unsupported target type: {route_config['target_type']}")

        route_properties = {
            "RouteTableId": route_table_id,
            "DestinationCidrBlock": route_config["destination_cidr_block"]
        }

        if route_config["target_type"] == "FirewallEndpoint":
            route_properties["VpcEndpointId"] = target_id
        elif route_config["target_type"] == "TGW":
            route_properties["TransitGatewayId"] = target_id

        fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::EC2::Route",
            f"{route_config['route_table_name']}-{route_config['destination_cidr_block']}",
            route_config["account_id"],
            route_config["region"],
            properties=route_properties
        )
        print(f"Route configured: {route_config['destination_cidr_block']} -> {target_id}")


def fetch_attachment_metadata(attachment_name):
    """Fetch metadata for either a TransitGatewayAttachment or TransitGatewayPeeringAttachment."""
    metadata = fetch_resource_by_name_and_type(attachment_name, "AWS::EC2::TransitGatewayAttachment")
    if not metadata:
        print(f"{attachment_name} not found as TransitGatewayAttachment, trying TransitGatewayPeeringAttachment...")
        metadata = fetch_resource_by_name_and_type(attachment_name, "AWS::EC2::TransitGatewayPeeringAttachment")
    return metadata

def configure_tgw_associations_and_propagations():
    """Configure TGW Route Table Associations and Propagations."""
    config = load_yaml_config("tgw-setup.yaml")

    for association_config in config["tgw_attachment_associations"]:
        session = assume_role(association_config["account_id"])
        cloudcontrol_client = session.client('cloudcontrol', region_name=association_config["region"])
        ec2_client = session.client("ec2", region_name=association_config["region"])

        # Fetch Transit Gateway ID
        print(f"Fetching Transit Gateway ID for {association_config['tgw_name']}...")
        tgw_metadata = fetch_resource_by_name_and_type(
            association_config["tgw_name"], "AWS::EC2::TransitGateway"
        )
        if not tgw_metadata:
            raise Exception(f"Transit Gateway {association_config['tgw_name']} not found in DynamoDB.")

        tgw_id = tgw_metadata["ResourceId"]

        # Fetch or Create TGW Route Table
        print(f"Fetching TGW Route Table ID for {association_config['tgw_route_table_name']}...")
        tgw_route_table_metadata = fetch_resource_by_name_and_type(
            association_config["tgw_route_table_name"], "AWS::EC2::TransitGatewayRouteTable"
        )
        if not tgw_route_table_metadata:
            print(f"TGW Route Table {association_config['tgw_route_table_name']} not found. Creating it...")
            tgw_route_table_properties = {
                "TransitGatewayId": tgw_id,
                "Tags": [{"Key": "Name", "Value": association_config["tgw_route_table_name"]}]
            }
            tgw_route_table_id = fetch_or_create_resource(
                cloudcontrol_client,
                "AWS::EC2::TransitGatewayRouteTable",
                association_config["tgw_route_table_name"],
                association_config["account_id"],
                association_config["region"],
                properties=tgw_route_table_properties
            )
            print(f"TGW Route Table created: {association_config['tgw_route_table_name']} -> {tgw_route_table_id}")
        else:
            tgw_route_table_id = tgw_route_table_metadata["ResourceId"]

        # Store TGW Route Table in DynamoDB if newly created
        store_resource_mapping(
            resource_id=tgw_route_table_id,
            resource_name=association_config["tgw_route_table_name"],
            resource_type="AWS::EC2::TransitGatewayRouteTable",
            account_id=association_config["account_id"],
            region=association_config["region"],
            metadata={"tgw_id": tgw_id}
        )

        if "associated_attachment" in association_config:
            # Handle Associations
            print(f"Associating TGW Attachments with Route Table {association_config['tgw_route_table_name']}...")
            for attachment_name in association_config["associated_attachment"]:
                attachment_metadata = fetch_attachment_metadata(attachment_name)
                if not attachment_metadata:
                    raise Exception(f"TGW Attachment {attachment_name} not found in DynamoDB.")
                attachment_id = attachment_metadata["ResourceId"]

                print(f"Checking current association of Attachment {attachment_name}...")
                current_association = ec2_client.describe_transit_gateway_attachments(
                    TransitGatewayAttachmentIds=[attachment_id]
                )["TransitGatewayAttachments"][0].get("Association", {})

                if current_association.get("TransitGatewayRouteTableId") != tgw_route_table_id:
                    if current_association.get("TransitGatewayRouteTableId"):
                        print(f"Disassociating Attachment {attachment_name} from current Route Table...")
                        disassociate_tgw_route_table(ec2_client, attachment_id)
                        time.sleep(30)

                    print(f"Associating Attachment {attachment_name} with Route Table {association_config['tgw_route_table_name']}...")
                    fetch_or_create_resource(
                        cloudcontrol_client,
                        "AWS::EC2::TransitGatewayRouteTableAssociation",
                        f"{association_config['tgw_route_table_name']}-{attachment_name}-Association",
                        association_config["account_id"],
                        association_config["region"],
                        properties={
                            "TransitGatewayRouteTableId": tgw_route_table_id,
                            "TransitGatewayAttachmentId": attachment_id
                        }
                    )
                    print(f"Associated: {attachment_name} -> {association_config['tgw_route_table_name']}.")
                else:
                    print(f"Attachment {attachment_name} is already associated with Route Table {association_config['tgw_route_table_name']}.")

        if "propagated_attachments" in association_config:
            # Handle Propagations
            print(f"Configuring propagations for Route Table {association_config['tgw_route_table_name']}...")
            for propagated_attachment_name in association_config["propagated_attachments"]:
                propagated_attachment_metadata = fetch_attachment_metadata(propagated_attachment_name)
                if not propagated_attachment_metadata:
                    raise Exception(f"TGW Attachment {propagated_attachment_name} not found in DynamoDB.")
                propagated_attachment_id = propagated_attachment_metadata["ResourceId"]

                fetch_or_create_resource(
                    cloudcontrol_client,
                    "AWS::EC2::TransitGatewayRouteTablePropagation",
                    f"{association_config['tgw_route_table_name']}-{propagated_attachment_name}-Propagation",
                    association_config["account_id"],
                    association_config["region"],
                    properties={
                        "TransitGatewayRouteTableId": tgw_route_table_id,
                        "TransitGatewayAttachmentId": propagated_attachment_id
                    }
                )
                print(f"Propagated: {propagated_attachment_name} -> {association_config['tgw_route_table_name']}.")
        
        if "routes" in association_config:
            # Handle Routes for Propagated Attachments
            print(f"Configuring routes for Route Table {association_config['tgw_route_table_name']}...")
            for route in association_config.get("routes", []):
                target_attachment_metadata = fetch_attachment_metadata(route["target_attachment_name"])
                if not target_attachment_metadata:
                    raise Exception(f"Target TGW Attachment {route['target_attachment_name']} not found.")
                target_attachment_id = target_attachment_metadata["ResourceId"]

                route_properties = {
                    "TransitGatewayRouteTableId": tgw_route_table_id,
                    "DestinationCidrBlock": route["destination_cidr_block"],
                    "TransitGatewayAttachmentId": target_attachment_id
                }
                fetch_or_create_resource(
                    cloudcontrol_client,
                    "AWS::EC2::TransitGatewayRoute",
                    f"{association_config['tgw_route_table_name']}-{route['destination_cidr_block']}",
                    association_config["account_id"],
                    association_config["region"],
                    properties=route_properties
                )
                print(f"Route added: {route['destination_cidr_block']} -> {target_attachment_id}")


# # Step 2: Deploy Network Firewalls and Policies
def deploy_tgw_resources():
    """Deploy VPCs, Subnets, Transit Gateways, TGW Attachments, and Route Tables."""
    config = load_yaml_config("tgw-setup.yaml")

    for vpc_config in config["vpcs"]:
        session = assume_role(vpc_config["account_id"])
        cloudcontrol_client = session.client('cloudcontrol', region_name=vpc_config["region"])

        # Create VPC
        print(f"Deploying VPC: {vpc_config['name']} in {vpc_config['region']}...")
        vpc_properties = {
            "CidrBlock": vpc_config["cidr_block"],
            "Tags": [{"Key": "Name", "Value": vpc_config["name"]}]
        }
        vpc_id = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::EC2::VPC",
            vpc_config["name"],
            vpc_config["account_id"],
            vpc_config["region"],
            properties=vpc_properties
        )
        print(f"VPC deployed or fetched: {vpc_config['name']} -> {vpc_id}")

        # Create Subnets and Route Tables
        for subnet_config in vpc_config["subnets"]:
            print(f"Deploying Subnet: {subnet_config['name']} in VPC {vpc_config['name']}...")
            subnet_properties = {
                "VpcId": vpc_id,
                "CidrBlock": subnet_config["cidr_block"],
                "AvailabilityZone": subnet_config["availability_zone"],
                "Tags": [{"Key": "Name", "Value": subnet_config["name"]}]
            }
            subnet_id = fetch_or_create_resource(
                cloudcontrol_client,
                "AWS::EC2::Subnet",
                subnet_config["name"],
                vpc_config["account_id"],
                vpc_config["region"],
                properties=subnet_properties
            )
            print(f"Subnet deployed or fetched: {subnet_config['name']} -> {subnet_id}")

            # Create Route Table for Subnet
            if "route_table" in subnet_config:
                route_table_config = subnet_config["route_table"]
                print(f"Deploying Route Table: {route_table_config['name']} for Subnet {subnet_config['name']}...")
                route_table_properties = {
                    "VpcId": vpc_id,
                    "Tags": [{"Key": "Name", "Value": route_table_config["name"]}]
                }
                route_table_id = fetch_or_create_resource(
                    cloudcontrol_client,
                    "AWS::EC2::RouteTable",
                    route_table_config["name"],
                    vpc_config["account_id"],
                    vpc_config["region"],
                    properties=route_table_properties
                )
                print(f"Route Table deployed or fetched: {route_table_config['name']} -> {route_table_id}")

                # Associate Route Table with Subnet
                print(f"Associating Route Table {route_table_config['name']} with Subnet {subnet_config['name']}...")
                fetch_or_create_resource(
                    cloudcontrol_client,
                    "AWS::EC2::SubnetRouteTableAssociation",
                    f"{subnet_config['name']}-RT-Association",
                    vpc_config["account_id"],
                    vpc_config["region"],
                    properties={
                        "RouteTableId": route_table_id,
                        "SubnetId": subnet_id
                    }
                )
                print(f"Route Table {route_table_config['name']} associated with Subnet {subnet_config['name']}.")

    # Deploy Transit Gateways and Attachments
    for tgw_config in config.get("transit_gateways", []):
        session = assume_role(tgw_config["account_id"])
        cloudcontrol_client = session.client('cloudcontrol', region_name=tgw_config["region"])

        print(f"Deploying Transit Gateway: {tgw_config['name']} in {tgw_config['region']}...")
        tgw_properties = {
            "Description": tgw_config["description"],
            "AmazonSideAsn": tgw_config["AmazonSideAsn"],
            "Tags": [{"Key": "Name", "Value": tgw_config["name"]}]
        }
        tgw_id = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::EC2::TransitGateway",
            tgw_config["name"],
            tgw_config["account_id"],
            tgw_config["region"],
            properties=tgw_properties
        )
        print(f"Transit Gateway deployed or fetched: {tgw_config['name']} -> {tgw_id}")

        # Create TGW Attachments
        for tgw_attachment in config.get("tgw_attachments", []):
            if tgw_attachment["tgw_name"] == tgw_config["name"]:
                print(f"Deploying TGW Attachment: {tgw_attachment['name']}...")
                vpc_id = fetch_resource_by_name_and_type(
                    tgw_attachment["vpc_name"], "AWS::EC2::VPC"
                )["ResourceId"]
                subnet_ids = [
                    fetch_resource_by_name_and_type(subnet_name, "AWS::EC2::Subnet")["ResourceId"]
                    for subnet_name in tgw_attachment["subnets"]
                ]
                tgw_attachment_properties = {
                    "TransitGatewayId": tgw_id,
                    "VpcId": vpc_id,
                    "SubnetIds": subnet_ids,
                    "Tags": [{"Key": "Name", "Value": tgw_attachment["name"]}]
                }
                fetch_or_create_resource(
                    cloudcontrol_client,
                    "AWS::EC2::TransitGatewayAttachment",
                    tgw_attachment["name"],
                    tgw_config["account_id"],
                    tgw_config["region"],
                    properties=tgw_attachment_properties
                )
                print(f"TGW Attachment deployed: {tgw_attachment['name']}.")


# # Step 2: Deploy Network Firewalls and Policies
def deploy_network_inspection():
    """Deploy Network Inspection Resources."""
    config = load_yaml_config("network-firewall.yaml")

    for firewall in config["firewalls"]:
        account = firewall["account_id"]
        session = assume_role(account)
        cloudcontrol_client = session.client('cloudcontrol', region_name=firewall["region"] )
        region = firewall["region"]

        # Deploy Firewall Policy
        print(f"Deploying Firewall Policy: {firewall['firewall_policies']['name']} in {region}...")
        policy_properties = {
            "FirewallPolicyName": firewall["firewall_policies"]["name"],
            "FirewallPolicy": {
                "StatelessDefaultActions": firewall["firewall_policies"]["stateless_default_actions"],
                "StatelessFragmentDefaultActions": firewall["firewall_policies"]["stateless_fragment_default_actions"]
            },
            "Description": firewall["firewall_policies"]["description"]
        }
        policy_arn = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::NetworkFirewall::FirewallPolicy",
            firewall["firewall_policies"]["name"],
            account,
            region,
            properties=policy_properties
        )
        print(f"Firewall Policy deployed or fetched: {firewall['firewall_policies']['name']} -> {policy_arn}")

        # Fetch or create VPC
        print(f"Fetching VPC: {firewall['vpc_name']} in {region}...")
        vpc_id = fetch_resource_by_name_and_type(
            firewall["vpc_name"], "AWS::EC2::VPC"
        )["ResourceId"]
        print(f"VPC fetched: {firewall['vpc_name']} -> {vpc_id}")

        # Fetch or create Subnet
        print(f"Fetching Subnet: {firewall['subnet_name']} in {region}...")
        subnet_id = fetch_resource_by_name_and_type(
            firewall["subnet_name"], "AWS::EC2::Subnet"
        )["ResourceId"]
        print(f"Subnet fetched: {firewall['subnet_name']} -> {subnet_id}")

        # Deploy Firewall
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

        # Store Firewall in DynamoDB
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


# Step 3: Share TGW via RAM
def deploy_tgw_sharing():
    """Share TGWs with the entire AWS Organization and store in DynamoDB."""
    config = load_yaml_config("tgw-sharing.yaml")
    tgw_shares = config["tgw_share"]

    for tgw_share_config in tgw_shares:
        session = assume_role(tgw_share_config["account_id"])
        cloudcontrol_client = session.client("cloudcontrol", region_name=tgw_share_config["region"])
        ram_client = session.client("ram", region_name=tgw_share_config["region"])

        print(f"Fetching TGW ID for {tgw_share_config['tgw_name']} in {tgw_share_config['region']}...")
        tgw_id = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::EC2::TransitGateway",
            tgw_share_config["tgw_name"],
            tgw_share_config["account_id"],
            tgw_share_config["region"]
        )
        tgw_arn = f"arn:aws:ec2:{tgw_share_config['region']}:{tgw_share_config['account_id']}:transit-gateway/{tgw_id}"
        print(f"TGW ARN: {tgw_arn}")

        # Check for existing resource share in DynamoDB
        existing_share = fetch_resource_by_name_and_type(
            f"TGW-ResourceShare-{tgw_share_config['tgw_name']}",
            "AWS::RAM::ResourceShare"
        )
        if existing_share:
            print(f"Resource share already exists: {existing_share['ResourceId']}")
            continue

        try:
            print(f"Creating or fetching Resource Share for TGW '{tgw_share_config['tgw_name']}'...")
            
            resource_share_properties = {
                "name": f"TGW-ResourceShare-{tgw_share_config['tgw_name']}",
                "resourceArns": [tgw_arn],
                "principals": [tgw_share_config["organization_arn"]],
                "allowExternalPrincipals": False,
                "tags": [
                    {"Key": "Name", "Value": f"TGW-Share-{tgw_share_config['tgw_name']}"}
                ]
            }

            # Fetch or create the Resource Share
            resource_share_id = fetch_or_create_resource(
                cloudcontrol_client,
                "AWS::RAM::ResourceShare",
                f"TGW-ResourceShare-{tgw_share_config['tgw_name']}",
                tgw_share_config["account_id"],
                tgw_share_config["region"],
                properties=resource_share_properties
            )
            print(f"Resource Share created or fetched successfully: {resource_share_id}")

        except Exception as e:
            print(f"Failed to create Resource Share for TGW '{tgw_share_config['tgw_name']}': {e}")
        #     print(f"Creating Resource Share to share TGW '{tgw_share_config['tgw_name']}' with AWS Organization...")
        #     response = ram_client.create_resource_share(
        #         name=f"TGW-ResourceShare-{tgw_share_config['tgw_name']}",
        #         resourceArns=[tgw_arn],
        #         principals=[tgw_share_config["organization_arn"]],
        #         allowExternalPrincipals=False,
        #         tags=[{"key": "Name", "value": f"TGW-Share-{tgw_share_config['tgw_name']}"}]
        #     )
        #     resource_share_arn = response["resourceShare"]["resourceShareArn"]
        #     print(f"Resource Share created successfully: {resource_share_arn}")

        #     # Store the Resource Share in DynamoDB
        #     store_resource_mapping(
        #         resource_id=resource_share_arn,
        #         resource_name=f"TGW-ResourceShare-{tgw_share_config['tgw_name']}",
        #         resource_type="AWS::RAM::ResourceShare",
        #         account_id=tgw_share_config["account_id"],
        #         region=tgw_share_config["region"],
        #         metadata={
        #             "tgw_name": tgw_share_config["tgw_name"],
        #             "tgw_arn": tgw_arn,
        #             "organization_arn": tgw_share_config["organization_arn"]
        #         }
        #     )
        #     print(f"Resource Share stored in DynamoDB: {resource_share_arn}")
        # except Exception as e:
        #     print(f"Failed to create Resource Share for TGW '{tgw_share_config['tgw_name']}': {e}")


# # Step 4: Deploy Workload VPCs and Subnets
def deploy_workload_setup():
    """Deploy workload VPCs, Subnets, and TGW Attachments, and store resource IDs in DynamoDB."""
    config = load_yaml_config("workload-vpc.yaml")

    for workload_vpc in config["workload_vpcs"]:
        session = assume_role(workload_vpc["account_id"])
        cloudcontrol_client = session.client("cloudcontrol", region_name=workload_vpc["region"])
        ec2_client = session.client("ec2", region_name=workload_vpc["region"] )

        # Create VPC
        vpc_properties = {
            "CidrBlock": workload_vpc["cidr_block"],
            "Tags": [{"Key": "Name", "Value": workload_vpc["vpc_name"]}]
        }
        vpc_id = fetch_or_create_resource(
            cloudcontrol_client,
            "AWS::EC2::VPC",
            workload_vpc["vpc_name"],
            workload_vpc["account_id"],
            workload_vpc["region"],
            properties=vpc_properties
        )
        print(f"Workload VPC deployed: {workload_vpc['vpc_name']} -> {vpc_id}")

        # Store VPC in DynamoDB
        store_resource_mapping(
            resource_id=vpc_id,
            resource_name=workload_vpc["vpc_name"],
            resource_type="AWS::EC2::VPC",
            account_id=workload_vpc["account_id"],
            region=workload_vpc["region"],
            metadata={"cidr_block": workload_vpc["cidr_block"]}
        )

        # Create Subnets and Route Tables
        for subnet_config in workload_vpc["subnets"]:
            subnet_properties = {
                "VpcId": vpc_id,
                "CidrBlock": subnet_config["subnet_cidr"],
                "AvailabilityZone": subnet_config["availability_zone"],
                "Tags": [{"Key": "Name", "Value": subnet_config["name"]}]
            }
            subnet_id = fetch_or_create_resource(
                cloudcontrol_client,
                "AWS::EC2::Subnet",
                subnet_config["name"],
                workload_vpc["account_id"],
                workload_vpc["region"],
                properties=subnet_properties
            )
            print(f"Subnet deployed: {subnet_config['name']} -> {subnet_id}")

            # Store Subnet in DynamoDB
            store_resource_mapping(
                resource_id=subnet_id,
                resource_name=subnet_config["name"],
                resource_type="AWS::EC2::Subnet",
                account_id=workload_vpc["account_id"],
                region=workload_vpc["region"],
                metadata={"vpc_id": vpc_id, "cidr_block": subnet_config["subnet_cidr"]}
            )

            # Create Route Table if specified
            if "route_table" in subnet_config:
                route_table_properties = {
                    "VpcId": vpc_id,
                    "Tags": [{"Key": "Name", "Value": subnet_config["route_table"]["name"]}]
                }
                route_table_id = fetch_or_create_resource(
                    cloudcontrol_client,
                    "AWS::EC2::RouteTable",
                    subnet_config["route_table"]["name"],
                    workload_vpc["account_id"],
                    workload_vpc["region"],
                    properties=route_table_properties
                )
                print(f"Route Table deployed: {subnet_config['route_table']['name']} -> {route_table_id}")

                # Associate Route Table with Subnet
                fetch_or_create_resource(
                    cloudcontrol_client,
                    "AWS::EC2::SubnetRouteTableAssociation",
                    f"{subnet_config['name']}-Association",
                    workload_vpc["account_id"],
                    workload_vpc["region"],
                    properties={
                        "RouteTableId": route_table_id,
                        "SubnetId": subnet_id
                    }
                )
                print(f"Route Table {subnet_config['route_table']['name']} associated with {subnet_config['name']}")

                # Store Route Table in DynamoDB
                store_resource_mapping(
                    resource_id=route_table_id,
                    resource_name=subnet_config["route_table"]["name"],
                    resource_type="AWS::EC2::RouteTable",
                    account_id=workload_vpc["account_id"],
                    region=workload_vpc["region"],
                    metadata={"vpc_id": vpc_id}
                )

                 # Add Routes to Route Table
                if "routes" in subnet_config["route_table"]:
                    for route in subnet_config["route_table"]["routes"]:
                        target_attachment = fetch_resource_by_name_and_type(
                            route["target"],
                            "AWS::EC2::TransitGateway"
                        )
                        if not target_attachment:
                            raise Exception(f"Transit Gateway Attachment {route['target']} not found.")

                        # Check if the route already exists
                        existing_routes = ec2_client.describe_route_tables(
                            RouteTableIds=[route_table_id]
                        )["RouteTables"][0]["Routes"]
                        route_exists = any(
                            r["DestinationCidrBlock"] == route["destination_cidr_block"]
                            for r in existing_routes
                        )
                        if route_exists:
                            print(f"Route {route['destination_cidr_block']} already exists in {subnet_config['route_table']['name']}. Skipping.")
                            continue

                        # Create the route
                        fetch_or_create_resource(
                            cloudcontrol_client,
                            "AWS::EC2::Route",
                            f"{subnet_config['route_table']['name']}-{route['destination_cidr_block']}",
                            workload_vpc["account_id"],
                            workload_vpc["region"],
                            properties={
                                "RouteTableId": route_table_id,
                                "DestinationCidrBlock": route["destination_cidr_block"],
                                "TransitGatewayId": target_attachment["ResourceId"]
                            }
                        )
                        print(f"Route added: {route['destination_cidr_block']} -> {target_attachment['ResourceId']}")

        # Create TGW Attachments
        for subnet_config in workload_vpc["subnets"]:
            if "transit-gateway-attachment" in subnet_config:
                for tgw_attachment_config in subnet_config["transit-gateway-attachment"]:
                    tgw_name = tgw_attachment_config["tgw_name"]
                    attachment_name = tgw_attachment_config["attachment_name"]

                    # Call create_and_accept_tgw_attachment
                    tgw_attachment_id = create_and_accept_tgw_attachment(
                            attachment_name=tgw_attachment_config["attachment_name"],
                            tgw_name=tgw_attachment_config["tgw_name"],
                            vpc_id=vpc_id,
                            subnet_ids=[subnet_id],
                            member_account_id=workload_vpc["account_id"],
                            region=workload_vpc["region"]
                        )
                    print(f"TGW Attachment created and accepted: {attachment_name} -> {tgw_attachment_id}")

                    # Store TGW Attachment in DynamoDB
                    store_resource_mapping(
                        resource_id=tgw_attachment_id,
                        resource_name=attachment_name,
                        resource_type="AWS::EC2::TransitGatewayAttachment",
                        account_id=workload_vpc["account_id"],
                        region=workload_vpc["region"],
                        metadata={"tgw_name": tgw_name, "vpc_id": vpc_id, "subnet_id": subnet_id}
                    )
            

def deploy_tgw_peering():
    """Deploy TGW Peering between regions and configure routes."""
    config = load_yaml_config("tgw-peering.yaml")

    for peering_config in config["tgw_peering"]:
        session1 = assume_role(peering_config["peer_account_id"])
        ec2_client1 = session1.client("ec2", region_name=peering_config["region1"])
        ec2_client2 = session1.client("ec2", region_name=peering_config["region2"])
        cloudcontrol_client1 = session1.client("cloudcontrol", region_name=peering_config["region1"])
        cloudcontrol_client2 = session1.client("cloudcontrol", region_name=peering_config["region2"])

        # Fetch TGW IDs
        print(f"Fetching TGW ID for {peering_config['tgw_name1']} in {peering_config['region1']}...")
        tgw_id_1 = fetch_resource_by_name_and_type(peering_config["tgw_name1"], "AWS::EC2::TransitGateway")["ResourceId"]

        print(f"Fetching TGW ID for {peering_config['tgw_name2']} in {peering_config['region2']}...")
        tgw_id_2 = fetch_resource_by_name_and_type(peering_config["tgw_name2"], "AWS::EC2::TransitGateway")["ResourceId"]

        # Check for existing peering attachment in DynamoDB
        existing_attachment = fetch_resource_by_name_and_type(peering_config["peering_name"], "AWS::EC2::TransitGatewayPeeringAttachment")
        if existing_attachment:
            peering_attachment_id = existing_attachment["ResourceId"]
            print(f"Using existing peering attachment: {peering_attachment_id}")
        else:
            # Check for existing attachment using EC2 API
            existing_attachments = ec2_client1.describe_transit_gateway_peering_attachments(
                Filters=[
                    {"Name": "transit-gateway-id", "Values": [tgw_id_1]},
                    {"Name": "transit-gateway-id", "Values": [tgw_id_2]},
                    {"Name": "state", "Values": ["available", "pendingAcceptance"]}
                ]
            )
            if existing_attachments.get("TransitGatewayPeeringAttachments"):
                peering_attachment_id = existing_attachments["TransitGatewayPeeringAttachments"][0]["TransitGatewayAttachmentId"]
                print(f"Existing peering attachment found: {peering_attachment_id}")
            else:
                # Create TGW Peering Attachment
                print(f"Creating TGW Peering Attachment between {tgw_id_1} and {tgw_id_2}...")
                peering_response = ec2_client1.create_transit_gateway_peering_attachment(
                    TransitGatewayId=tgw_id_1,
                    PeerTransitGatewayId=tgw_id_2,
                    PeerAccountId=peering_config["peer_account_id"],
                    PeerRegion=peering_config["region2"],
                    TagSpecifications=[
                        {
                            "ResourceType": "transit-gateway-attachment",
                            "Tags": [{"Key": "Name", "Value": peering_config["peering_name"]}]
                        }
                    ]
                )
                peering_attachment_id = peering_response["TransitGatewayPeeringAttachment"]["TransitGatewayAttachmentId"]
                print(f"Peering attachment created: {peering_attachment_id}")

                # Store the new attachment in DynamoDB
                store_resource_mapping(
                    resource_id=peering_attachment_id,
                    resource_name=peering_config["peering_name"],
                    resource_type="AWS::EC2::TransitGatewayPeeringAttachment",
                    account_id=peering_config["peer_account_id"],
                    region=peering_config["region1"],
                    metadata={"tgw_id_1": tgw_id_1, "tgw_id_2": tgw_id_2}
                )

        # Check the state before attempting to accept
        attachment_status = ec2_client1.describe_transit_gateway_peering_attachments(
            TransitGatewayAttachmentIds=[peering_attachment_id]
        )["TransitGatewayPeeringAttachments"][0]["State"]

        if attachment_status == "pendingAcceptance" or attachment_status == "initiatingRequest":
            # Accept TGW Peering Attachment in the peer region
            print(f"Accepting TGW Peering Attachment {peering_attachment_id} in region {peering_config['region2']}...")
            ec2_client2.accept_transit_gateway_peering_attachment(
                TransitGatewayAttachmentId=peering_attachment_id
            )
            print(f"TGW Peering Attachment {peering_attachment_id} accepted successfully.")
        elif attachment_status == "available":
            print(f"Peering attachment {peering_attachment_id} is already in 'available' state. Skipping acceptance.")
        else:
            raise Exception(f"Unexpected state for peering attachment {peering_attachment_id}: {attachment_status}")


# Full deployment entry point
def full_deployment():
    """Run all deployment steps."""
    print("Starting Step 1: VPCs, TGWs, and Attachments")
    deploy_tgw_resources()

    print("Starting Step 2: TGW Peering")
    deploy_tgw_peering()

    print("Starting Step 3: Network Firewalls and Policies")
    deploy_network_inspection()

    print("Starting Step 4 : Inspection VPC route table configuration for Inspection")
    inspection_vpc_routes()

    print("Starting Step 5: TGW Sharing within the Org")
    deploy_tgw_sharing()

    print("Starting Step 5: Workload VPCs, Subnets, and TGW Attachments")
    deploy_workload_setup()

    print("Starting Step 6: TGW attachment route table configuration to send traffic to inspection VPC")
    #create_tgw_attachment_route_tables()
    configure_tgw_associations_and_propagations()


if __name__ == "__main__":
    full_deployment()
