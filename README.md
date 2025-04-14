# sample-network-deployment-using-aws-cloud-control

# Streamlining Network Deployment using AWS Cloud Control

## Overview
This repository provides a comprehensive solution for deploying a multi-account, multi-region AWS network infrastructure. It uses the AWS **Cloud Control API**, **DynamoDB**, and **CodePipeline** to deploy and manage resources in a centralized and automated manner.

---

## Deployment Details

### **AWS Accounts**
We will require at minimum 4 AWS accounts to deploy this pattern:
- Deployment Account : AWS account responsible for running the AWS CodePipeline.
- Centralized Network Account : AWS account where the Network Firewall Inspection will happen using Centralized AWS Transit Gateway.
- Workload Account 1 : AWS account where your sample workload will run.
- Workload Account 2 : AWS account where your sample workload will run.

---
## Repository Contents

### **Python Script**
#### `cc-deployment-script.py`
The primary deployment script that automates the deployment process by:
- Reading configuration files.
- Deploying resources using AWS Cloud Control API.
- Storing resource metadata in **DynamoDB**.

---

### **YAML Configuration Files**
These files define the architecture and settings for resource deployment.

#### 1. **`tgw-setup.yaml`**
- **Purpose**: 
  - Deploys foundational networking resources in the Centralized Networking Account, including:
    - Inspection VPCs
    - Subnets
    - Transit Gateways (TGWs)
    - TGW attachments and route propagations
- **Usage**: 
  - The script reads this file to configure all networking resources in the central account.

#### 2. **`network-firewall.yaml`**
- **Purpose**: 
  - Deploys **AWS Network Firewalls** and policies for traffic inspection in the Centralized Networking Account.
- **Usage**: 
  - The script uses this file to create:
    - Firewall policies
    - Stateful and stateless rule groups
    - Network firewalls.

#### 3. **`tgw-sharing.yaml`**
- **Purpose**: 
  - Shares Transit Gateways (TGWs) across all accounts in the AWS Organization using **AWS RAM**.
- **Usage**: 
  - The script uses this file to share TGWs across accounts within the organization.

#### 4. **`workload-vpc.yaml`**
- **Purpose**: 
  - Defines workload VPCs, subnets, and TGW attachments in member accounts.
- **Usage**: 
  - The script reads this file to create workload VPCs and establish connectivity with TGWs.

#### 5. **`tgw-peering.yaml`**
- **Purpose**: 
  - Establishes TGW Peering Attachments between TGWs in different regions for inter-region communication.
- **Usage**: 
  - The script reads this file to set up peering connections and configure routes.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file - [LICENSE](./LICENSE) file for details.