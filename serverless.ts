import { AWS } from "@serverless/typescript";

import integrations from "./endpoints/integrations";
import vaults from "./endpoints/vaults";
import welcome from "./endpoints/welcome";
import jobs from "./jobs";

module.exports = <AWS>{
  service: "yearn-data",
  frameworkVersion: "2",
  package: {
    individually: true,
  },
  custom: {
    "serverless-offline": {
      noPrependStageInUrl: true,
    },
    webpack: {
      webpackConfig: "webpack.config.js",
      includeModules: true,
    },
    customDomain: {
      domainName: "${self:custom.${opt:stage, self:provider.stage}.domain}",
      createRoute53Record: true,
      basePath: "",
      stage: "${opt:stage, self:provider.stage}",
    },
    apiGatewayCaching: {
      enabled: true,
      ttlInSeconds: 600,
    },
    experimental: {
      domain: "experimental.vaults.finance",
      rate: "rate(1 day)",
    },
    dev: {
      domain: "dev.vaults.finance",
      rate: "rate(10 minutes)",
    },
    prod: {
      domain: "vaults.finance",
      rate: "rate(10 minutes)",
    },
  },
  plugins: [
    "serverless-webpack",
    "serverless-offline",
    "serverless-dotenv-plugin",
    "serverless-domain-manager",
    "serverless-api-gateway-caching",
  ],
  provider: {
    name: "aws",
    runtime: "nodejs12.x",
    region: "us-east-1",
    stage: "${opt:stage, 'dev'}",
    apiGateway: {
      minimumCompressionSize: 1024,
      shouldStartNameWithService: true,
    },
    lambdaHashingVersion: "20201221",
    logRetentionInDays: 3,
    environment: {
      AWS_NODEJS_CONNECTION_REUSE_ENABLED: "1",
      DDB_VAULTS_CACHE:
        "${self:service}-${opt:stage, self:provider.stage}-VaultsCache",
    },
    iam: {
      role: {
        statements: [
          {
            Effect: "Allow",
            Action: [
              "dynamodb:Query",
              "dynamodb:Scan",
              "dynamodb:GetItem",
              "dynamodb:PutItem",
              "dynamodb:UpdateItem",
              "dynamodb:DeleteItem",
              "dynamodb:BatchGetItem",
              "dynamodb:BatchWriteItem",
            ],
            Resource:
              "arn:aws:dynamodb:${opt:region, self:provider.region}:*:table/${self:provider.environment.DDB_VAULTS_CACHE}",
          },
        ],
      },
    },
  },
  functions: {
    welcome,
    "vaults-list": vaults.list,
    "vaults-by-address": vaults.address,
    "integration-coingecko-pools": integrations.coingecko.pools,
    "job-vaults": jobs.vaults,
  },
  resources: {
    Resources: {
      VaultsCache: {
        Type: "AWS::DynamoDB::Table",
        DeletionPolicy: "Retain",
        Properties: {
          AttributeDefinitions: [
            {
              AttributeName: "address",
              AttributeType: "S",
            },
          ],
          KeySchema: [
            {
              AttributeName: "address",
              KeyType: "HASH",
            },
          ],
          ProvisionedThroughput: {
            ReadCapacityUnits: 5,
            WriteCapacityUnits: 5,
          },
          TableName: "${self:provider.environment.DDB_VAULTS_CACHE}",
        },
      },
    },
  },
};
