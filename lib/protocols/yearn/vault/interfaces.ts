export interface Token {
  symbol: string;
  address: string;
  decimals: number;
  name: string;
}

export interface Strategy {
  name: string;
  address: string;
}

export interface FeesV1 {
  strategistReward: number;
  performanceFee: number;
  withdrawalFee: number;
  treasuryFee: number;
  keepCRV: number;
}

export interface FeesV2 {
  performanceFee: number;
  managementFee: number;
}

export interface VaultBase {
  name: string;
  address: string;
  symbol: string;
  decimals: number;
  token: Token;
}

export interface VaultV1 extends VaultBase {
  type: "v1";
  strategies: Strategy[];
  fees: FeesV1;
}

export interface VaultV2 extends VaultBase {
  type: "v2";
  emergencyShutdown: boolean;
  apiVersion: string;
  strategies: Strategy[];
  tags: string[];
  fees: FeesV2;
}

export type Vault = VaultV1 | VaultV2;
