import type { components, paths } from "./schema.gen";

type S = components["schemas"];

export type Id = S["Id"];
export type Money = S["Money"];
export type LogicalTier = S["LogicalTier"];
export type PinTier = S["PinTier"];
export type Check = S["Check"];
export type GroupRef = S["GroupRef"];
export type PersonRef = S["PersonRef"];
export type Me = S["Me"];
export type Role = S["Role"];
export type Session = S["Session"];
export type Share = S["Share"];
export type ShareSettingsPatch = S["ShareSettingsPatch"];
export type ShareAccess = S["ShareAccess"];
export type WormState = S["WormState"];
export type TierState = S["TierState"];
export type Entry = S["Entry"];
export type AsOfEntry = S["AsOfEntry"];
export type DirHeader = S["DirHeader"];
export type PathSegment = S["PathSegment"];
export type FileDetail = S["FileDetail"];
export type Version = S["Version"];
export type RecallOptions = S["RecallOptions"];
export type RecallOption = S["RecallOption"];
export type RecallState = S["RecallState"];
export type Recall = S["Recall"];
export type RecallStep = S["RecallStep"];
export type PinPreview = S["PinPreview"];
export type Pin = S["Pin"];
export type Hold = S["Hold"];
export type Skipped = S["Skipped"];
export type VersionRestore = S["VersionRestore"];
export type Download = S["Download"];
export type PolicyBody = S["PolicyBody"];
export type NotifySettings = S["NotifySettings"];
export type PolicyVersion = S["PolicyVersion"];
export type PolicyPreview = S["PolicyPreview"];
export type PolicyPreviewRequest = S["PolicyPreviewRequest"];
export type ShareOverride = S["ShareOverride"];
export type CostAssumptions = S["CostAssumptions"];
export type TimelineSegment = S["TimelineSegment"];
export type Proposal = S["Proposal"];
export type ProposalState = S["ProposalState"];
export type Dataset = S["Dataset"];
export type Backend = S["Backend"];
export type NewShare = S["NewShare"];
export type NewDataset =
  paths["/v1/datasets"]["post"]["requestBody"]["content"]["application/json"];
export type AuditRecord = S["AuditRecord"];
export type Problem = S["Problem"];
export type ProblemCode = Problem["code"];
export type OnOpen = Share["on_open"];
export type NfsSec = Share["nfs_sec"][number];

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
