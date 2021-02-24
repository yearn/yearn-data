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

export interface SpecialFeesV1 {
  keepCrv?: number;
}

export interface GeneralFeesV1 {
  withdrawalFee: number;
  performanceFee: number;
}

export interface FeesV1 {
  general: GeneralFeesV1;
  special: SpecialFeesV1;
}

export interface SpecialFeesV2 {
  keepCrv?: number;
  strategistReward?: number;
  treasuryFee?: number;
}

export interface GeneralFeesV2 {
  performanceFee: number;
  managementFee: number;
}

export interface FeesV2 {
  general: GeneralFeesV2;
  special: SpecialFeesV2;
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
