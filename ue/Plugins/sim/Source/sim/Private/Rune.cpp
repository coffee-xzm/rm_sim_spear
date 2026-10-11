#include "Rune.h"

#include "Components/StaticMeshComponent.h"
#include "DrawDebugHelpers.h"
#include "Materials/MaterialInterface.h"
#include "RuneArmorTargetComponent.h"
#include "RuneHitMath.h"
#include "RuneVisualProfile.h"
#include "RuneVisualRules.h"

DEFINE_LOG_CATEGORY_STATIC(LogRuneVisual, Log, All);

ARune::ARune()
{
	PrimaryActorTick.bCanEverTick = false;
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));

	for (int32 Index = 0; Index < 5; ++Index)
	{
		URuneArmorTargetComponent* Target = CreateDefaultSubobject<URuneArmorTargetComponent>(
			*FString::Printf(TEXT("ArmorTarget_%d"), Index));
		Target->SetupAttachment(RootComponent);
		Target->SetCollisionEnabled(ECollisionEnabled::NoCollision);
		ArmorTargets.Add(Target);
	}
}

void ARune::BeginPlay()
{
	Super::BeginPlay();
	if (Binding.VisualRootActor && Binding.VisualRootActor->GetRootComponent())
	{
		Binding.BaselineRelativeTransform = Binding.VisualRootActor->GetRootComponent()->GetRelativeTransform();
	}
	ConfigureHitTargets();
	InitializeVisualSlots();
	RefreshVisuals();
}

void ARune::InitializeRules(ESceneMode Mode, int32 Seed)
{
	const ERuneState OldState = RuleState.GetSnapshot().State;
	RuleState.Reset(Mode, Seed);
	AutoHitScheduler.Reset();
	LastBroadcastState = RuleState.GetSnapshot().State;
	RefreshVisuals();
	if (OldState != LastBroadcastState)
	{
		OnRuneStateChanged.Broadcast(LastBroadcastState, OldState);
	}
}

void ARune::AdvanceRules(double DeltaSeconds, bool bAutoHitTestEnabled, double AutoHitDelaySeconds)
{
	const FRuneSnapshot OldSnapshot = RuleState.GetSnapshot();
	RuleState.AdvanceTime(DeltaSeconds);
	const int32 AutoHitArmorIndex = AutoHitScheduler.Advance(
		RuleState.GetSnapshot(), DeltaSeconds, bAutoHitTestEnabled, AutoHitDelaySeconds);
	if (AutoHitArmorIndex != INDEX_NONE)
	{
		const ERuneTransitionReason Result = RuleState.HandleHit(AutoHitArmorIndex, 0, 10);
		if (Result != ERuneTransitionReason::InvalidHit && Result != ERuneTransitionReason::IgnoredInactiveHit)
		{
			OnArmorHit.Broadcast(AutoHitArmorIndex, 10, TeamColor);
			UE_LOG(LogRuneVisual, Display, TEXT("Auto-hit test hit %s arm %d after %.3f seconds"),
				*GetName(), AutoHitArmorIndex, AutoHitDelaySeconds);
		}
	}
	const FRuneSnapshot& NewSnapshot = RuleState.GetSnapshot();
	if (OldSnapshot.State != NewSnapshot.State
		|| OldSnapshot.CompletedArmorIndices != NewSnapshot.CompletedArmorIndices
		|| OldSnapshot.LitArmorIndices != NewSnapshot.LitArmorIndices
		|| OldSnapshot.ArmorHitRings != NewSnapshot.ArmorHitRings
		|| OldSnapshot.bCompletionFlashOn != NewSnapshot.bCompletionFlashOn)
	{
		RefreshVisuals();
	}
	if (OldSnapshot.State != NewSnapshot.State)
	{
		OnRuneStateChanged.Broadcast(NewSnapshot.State, OldSnapshot.State);
	}
}

ERuneTransitionReason ARune::HandleArmorHit(int32 ArmorIndex, const FVector& ImpactPoint, ETeamColor ShooterTeam)
{
	if (!ArmorTargets.IsValidIndex(ArmorIndex) || !ArmorTargets[ArmorIndex])
	{
		return ERuneTransitionReason::InvalidHit;
	}

	const FRuneHitCalculation Hit = RuneHitMath::Calculate(
		ArmorTargets[ArmorIndex]->GetComponentTransform(), ImpactPoint, ArmorTargets[ArmorIndex]->GetTargetRadiusCm());
	if (!Hit.bValid)
	{
		return ERuneTransitionReason::InvalidHit;
	}

	const ERuneState OldState = RuleState.GetSnapshot().State;
	const ERuneTransitionReason Result = RuleState.HandleHit(ArmorIndex, Hit.RadiusMm, Hit.Ring);
	OnArmorHit.Broadcast(ArmorIndex, Hit.Ring, ShooterTeam);
	if (Hit.bValid)
	{
		const FColor HitColor = ShooterTeam == ETeamColor::Red ? FColor::Red : FColor::Blue;
		DrawDebugSphere(GetWorld(), ImpactPoint, 2.0f, 12, HitColor, false, 2.0f, 0, 1.5f);
		DrawDebugString(GetWorld(), ImpactPoint + FVector(0.0f, 0.0f, 4.0f),
			FString::Printf(TEXT("Arm %d  Ring %d  %d mm"), ArmorIndex, Hit.Ring, Hit.RadiusMm),
			nullptr, HitColor, 2.0f, true);
	}
	RefreshVisuals();
	if (OldState != RuleState.GetSnapshot().State)
	{
		OnRuneStateChanged.Broadcast(RuleState.GetSnapshot().State, OldState);
	}
	return Result;
}

void ARune::ApplySharedAngle(double AngleRad)
{
	if (!Binding.VisualRootActor)
	{
		return;
	}

	const FVector Axis = Binding.RotationAxisLocal.GetSafeNormal(UE_SMALL_NUMBER, FVector::RightVector);
	const FQuat Rotation(Axis, AngleRad * static_cast<double>(Binding.RotationSign));
	FTransform Transform = Binding.BaselineRelativeTransform;
	Transform.SetRotation(Binding.BaselineRelativeTransform.GetRotation() * Rotation);
	Binding.VisualRootActor->SetActorRelativeTransform(Transform);
}

void ARune::DrawDebugTargets() const
{
	for (const URuneArmorTargetComponent* Target : ArmorTargets)
	{
		if (!Target || !Target->IsTargetValid())
		{
			continue;
		}
		DrawDebugBox(GetWorld(), Target->GetComponentLocation(), Target->GetScaledBoxExtent(),
			Target->GetComponentQuat(), TeamColor == ETeamColor::Red ? FColor::Red : FColor::Blue,
			false, 0.0f, 0, 1.5f);
		DrawDebugCoordinateSystem(GetWorld(), Target->GetComponentLocation(), Target->GetComponentRotation(),
			12.0f, false, 0.0f, 0, 1.0f);
	}
}

void ARune::RefreshVisuals()
{
	if (!Binding.VisualRootActor)
	{
		return;
	}
	WarnForMissingVisualMaterials();

	const FRuneSnapshot& Snapshot = RuleState.GetSnapshot();
	// Activating is not a global active state. Unlit and unfinished parts stay metal;
	// only the current targets and completed parts receive luminous materials below.
	UMaterialInterface* BaseMaterial = VisualProfile ? VisualProfile->MetalMaterial.Get() : nullptr;
	ApplyMaterialToSlots(RootVisualSlots, BaseMaterial);
	// centerR is off only when neither energy-mechanism mode is selected.
	const bool bCenterInactive = Snapshot.Mode == ESceneMode::Inactive;
	ApplyMaterialToSlots(CenterVisualSlots, GetCenterMaterial(bCenterInactive), true);
	auto ResolveLightMaterial = [this, BaseMaterial](ERuneResolvedLightMaterial State)
	{
		switch (State)
		{
		case ERuneResolvedLightMaterial::Active:
			return GetActiveMaterial();
		case ERuneResolvedLightMaterial::Arrow:
			return GetArrowMaterial();
		case ERuneResolvedLightMaterial::Full:
			return GetFullMaterial();
		default:
			return BaseMaterial;
		}
	};

	for (int32 ArmIndex = 0; ArmIndex < ArmVisualSlots.Num(); ++ArmIndex)
	{
		const int32 HitRing = Snapshot.ArmorHitRings.IsValidIndex(ArmIndex)
			? Snapshot.ArmorHitRings[ArmIndex] : 0;
		const FRuneArmLightVisualState LightState = RuneVisualRules::ResolveArmLights(Snapshot, ArmIndex);
		UMaterialInterface* HorizontalMaterial = ResolveLightMaterial(LightState.Horizontal);
		UMaterialInterface* VerticalMaterial = ResolveLightMaterial(LightState.Vertical);
		UMaterialInterface* FanMaterial = BaseMaterial;
		switch (RuneVisualRules::ResolveFan(Snapshot, ArmIndex))
		{
		case ERuneResolvedFanMaterial::Active:
			FanMaterial = GetFanActiveMaterial();
			break;
		case ERuneResolvedFanMaterial::Ring:
			FanMaterial = GetFanRingMaterial(HitRing);
			break;
		default:
			break;
		}
		ApplyMaterialToSlots(ArmVisualSlots[ArmIndex].FanSlots, FanMaterial);
		ApplyMaterialToSlots(ArmVisualSlots[ArmIndex].HorizontalLightSlots, HorizontalMaterial);
		ApplyMaterialToSlots(ArmVisualSlots[ArmIndex].VerticalLightSlots, VerticalMaterial);
		UMaterialInterface* FlowMaterial = ResolveLightMaterial(LightState.Flow);
		ApplyMaterialToSlots(ArmVisualSlots[ArmIndex].FlowSlots, FlowMaterial);
	}
}

bool ARune::ValidateBinding(TArray<FString>& OutErrors) const
{
	TSet<const AActor*> AxisActors;
	TSet<const AActor*> ArmorActors;
	TSet<const AActor*> RoleActors;
	if (!Binding.VisualRootActor)
	{
		OutErrors.Add(TEXT("Missing VisualRootActor"));
	}
	else if (!Binding.VisualRootActor->GetRootComponent()
		|| Binding.VisualRootActor->GetRootComponent()->Mobility != EComponentMobility::Movable)
	{
		OutErrors.Add(TEXT("VisualRootActor root component must be Movable"));
	}
	if (!Binding.CenterRootActor)
	{
		OutErrors.Add(TEXT("Missing CenterRootActor"));
	}
	else if (!Binding.CenterRootActor->GetRootComponent()
		|| Binding.CenterRootActor->GetRootComponent()->Mobility != EComponentMobility::Movable)
	{
		OutErrors.Add(TEXT("CenterRootActor root component must be Movable"));
	}
	if (Binding.Arms.Num() != 5)
	{
		OutErrors.Add(FString::Printf(TEXT("Expected 5 arms, got %d"), Binding.Arms.Num()));
	}
	for (int32 Index = 0; Index < Binding.Arms.Num(); ++Index)
	{
		const FRuneArmBinding& Arm = Binding.Arms[Index];
		if (!Arm.AxisActor || !Arm.ArmorAnchorActor)
		{
			OutErrors.Add(FString::Printf(TEXT("Arm %d is missing axis or armor anchor"), Index));
		}
		if (Arm.AxisActor && AxisActors.Contains(Arm.AxisActor))
		{
			OutErrors.Add(FString::Printf(TEXT("Arm %d reuses an axis actor"), Index));
		}
		if (Arm.ArmorAnchorActor && ArmorActors.Contains(Arm.ArmorAnchorActor))
		{
			OutErrors.Add(FString::Printf(TEXT("Arm %d reuses an armor anchor"), Index));
		}
		AxisActors.Add(Arm.AxisActor);
		ArmorActors.Add(Arm.ArmorAnchorActor);
		if (Arm.FanActors.Num() != 1 || Arm.FlowActors.Num() != 1 || Arm.LightActors.Num() != 4
			|| Arm.HorizontalLightActors.Num() != 2 || Arm.VerticalLightActors.Num() != 2)
		{
			OutErrors.Add(FString::Printf(TEXT("Arm %d expected fan=1 flow=1 lights=4 horizontal=2 vertical=2, got fan=%d flow=%d lights=%d horizontal=%d vertical=%d"),
				Index, Arm.FanActors.Num(), Arm.FlowActors.Num(), Arm.LightActors.Num(),
				Arm.HorizontalLightActors.Num(), Arm.VerticalLightActors.Num()));
		}
		auto ValidateRoleActors = [&OutErrors, &RoleActors, Index](
			const TCHAR* Role, const TArray<TObjectPtr<AActor>>& Actors)
		{
			for (const AActor* Actor : Actors)
			{
				if (!Actor)
				{
					OutErrors.Add(FString::Printf(TEXT("Arm %d has a null %s actor"), Index, Role));
				}
				else if (RoleActors.Contains(Actor))
				{
					OutErrors.Add(FString::Printf(TEXT("Arm %d reuses role actor %s"), Index, *Actor->GetName()));
				}
				else
				{
					RoleActors.Add(Actor);
				}
			}
		};
		ValidateRoleActors(TEXT("fan"), Arm.FanActors);
		ValidateRoleActors(TEXT("flow"), Arm.FlowActors);
		ValidateRoleActors(TEXT("light"), Arm.LightActors);
		TSet<const AActor*> ClassifiedLights;
		for (const AActor* Actor : Arm.HorizontalLightActors) ClassifiedLights.Add(Actor);
		for (const AActor* Actor : Arm.VerticalLightActors) ClassifiedLights.Add(Actor);
		for (const AActor* Actor : Arm.LightActors)
		{
			if (!ClassifiedLights.Contains(Actor))
			{
				OutErrors.Add(FString::Printf(TEXT("Arm %d has an unclassified light actor"), Index));
			}
		}
	}
	return OutErrors.IsEmpty();
}

bool ARune::ValidateVisualProfile(TArray<FString>& OutErrors) const
{
	if (!VisualProfile)
	{
		OutErrors.Add(FString::Printf(TEXT("%s is missing VisualProfile"), *GetName()));
		return false;
	}
	auto Require = [&OutErrors, this](const UMaterialInterface* Material, const TCHAR* Name)
	{
		if (!Material)
		{
			OutErrors.Add(FString::Printf(TEXT("%s VisualProfile is missing %s"), *GetName(), Name));
		}
	};
	Require(VisualProfile->MetalMaterial, TEXT("MetalMaterial"));
	Require(VisualProfile->RedActiveMaterial, TEXT("RedActiveMaterial"));
	Require(VisualProfile->BlueActiveMaterial, TEXT("BlueActiveMaterial"));
	Require(VisualProfile->CenterInactiveMaterial, TEXT("CenterInactiveMaterial"));
	Require(VisualProfile->RedCenterActiveMaterial, TEXT("RedCenterActiveMaterial"));
	Require(VisualProfile->BlueCenterActiveMaterial, TEXT("BlueCenterActiveMaterial"));
	Require(VisualProfile->RedFanActiveMaterial, TEXT("RedFanActiveMaterial"));
	Require(VisualProfile->BlueFanActiveMaterial, TEXT("BlueFanActiveMaterial"));
	Require(VisualProfile->RedArrowMaterial, TEXT("RedArrowMaterial"));
	Require(VisualProfile->BlueArrowMaterial, TEXT("BlueArrowMaterial"));
	Require(VisualProfile->RedFullMaterial ? VisualProfile->RedFullMaterial : VisualProfile->RedActiveMaterial, TEXT("RedFullMaterial (or RedActiveMaterial fallback)"));
	Require(VisualProfile->BlueFullMaterial ? VisualProfile->BlueFullMaterial : VisualProfile->BlueActiveMaterial, TEXT("BlueFullMaterial (or BlueActiveMaterial fallback)"));
	auto ValidateRings = [&OutErrors, this](const TArray<TObjectPtr<UMaterialInterface>>& Materials, const TCHAR* Name)
	{
		if (Materials.Num() != 10)
		{
			OutErrors.Add(FString::Printf(TEXT("%s %s must contain 10 materials, got %d"),
				*GetName(), Name, Materials.Num()));
			return;
		}
		for (int32 Index = 0; Index < Materials.Num(); ++Index)
		{
			if (!Materials[Index])
			{
				OutErrors.Add(FString::Printf(TEXT("%s %s[%d] is null"), *GetName(), Name, Index));
			}
		}
	};
	ValidateRings(VisualProfile->RedFanRingMaterials, TEXT("RedFanRingMaterials"));
	ValidateRings(VisualProfile->BlueFanRingMaterials, TEXT("BlueFanRingMaterials"));
	return OutErrors.IsEmpty();
}

void ARune::ConfigureHitTargets()
{
	for (int32 Index = 0; Index < ArmorTargets.Num(); ++Index)
	{
		URuneArmorTargetComponent* Target = ArmorTargets[Index];
		if (!Target || !Binding.Arms.IsValidIndex(Index) || !Binding.Arms[Index].ArmorAnchorActor)
		{
			if (Target)
			{
				Target->SetCollisionEnabled(ECollisionEnabled::NoCollision);
			}
			continue;
		}

		const FRuneArmBinding& Arm = Binding.Arms[Index];
		Target->AttachToComponent(Arm.ArmorAnchorActor->GetRootComponent(),
			FAttachmentTransformRules::SnapToTargetNotIncludingScale);
		Target->SetRelativeTransform(Arm.ArmorLocalTransform);
		Target->InitializeTarget(this, TeamColor, Index, Arm.ArmorRadiusCm, Arm.ArmorThicknessCm);
		Target->SetCollisionEnabled(Target->IsTargetValid() ? ECollisionEnabled::QueryOnly : ECollisionEnabled::NoCollision);
	}

	SetVisualCollisionResponse(Binding.VisualRootActor);
}

void ARune::InitializeVisualSlots()
{
	RootVisualSlots.Reset();
	CenterVisualSlots.Reset();
	ArmVisualSlots.SetNum(Binding.Arms.Num());
	CollectVisualSlots(Binding.VisualRootActor, false, RootVisualSlots);
	CollectVisualSlots(Binding.CenterRootActor, true, CenterVisualSlots);

	for (int32 ArmIndex = 0; ArmIndex < Binding.Arms.Num(); ++ArmIndex)
	{
		const FRuneArmBinding& Arm = Binding.Arms[ArmIndex];
		for (AActor* Actor : Arm.FanActors)
		{
			CollectVisualSlots(Actor, true, ArmVisualSlots[ArmIndex].FanSlots);
		}
		for (AActor* Actor : Arm.FlowActors)
		{
			CollectVisualSlots(Actor, true, ArmVisualSlots[ArmIndex].FlowSlots);
		}
		for (AActor* Actor : Arm.HorizontalLightActors)
		{
			CollectVisualSlots(Actor, true, ArmVisualSlots[ArmIndex].HorizontalLightSlots);
		}
		for (AActor* Actor : Arm.VerticalLightActors)
		{
			CollectVisualSlots(Actor, true, ArmVisualSlots[ArmIndex].VerticalLightSlots);
		}

		const FRuneArmVisualSlots& Slots = ArmVisualSlots[ArmIndex];
		if (Slots.FanSlots.IsEmpty() || Slots.FlowSlots.IsEmpty()
			|| Slots.HorizontalLightSlots.IsEmpty() || Slots.VerticalLightSlots.IsEmpty())
		{
			UE_LOG(LogRuneVisual, Error,
				TEXT("Visual binding is incomplete for %s arm %d: fan slots=%d, flow slots=%d, horizontal slots=%d, vertical slots=%d"),
				*GetName(), ArmIndex, Slots.FanSlots.Num(), Slots.FlowSlots.Num(),
				Slots.HorizontalLightSlots.Num(), Slots.VerticalLightSlots.Num());
		}
	}

	if (CenterVisualSlots.IsEmpty())
	{
		UE_LOG(LogRuneVisual, Error, TEXT("Center visual binding has no StaticMesh material slots for %s"), *GetName());
	}
}

void ARune::CollectVisualSlots(AActor* Actor, bool bRecursive, TArray<FRuneVisualSlot>& OutSlots)
{
	if (!Actor)
	{
		return;
	}

	TArray<UStaticMeshComponent*> Meshes;
	Actor->GetComponents<UStaticMeshComponent>(Meshes);
	for (UStaticMeshComponent* Mesh : Meshes)
	{
		for (int32 Slot = 0; Slot < Mesh->GetNumMaterials(); ++Slot)
		{
			const bool bAlreadyCollected = OutSlots.ContainsByPredicate(
				[Mesh, Slot](const FRuneVisualSlot& Item)
				{
					return Item.Component == Mesh && Item.MaterialIndex == Slot;
				});
			if (!bAlreadyCollected)
			{
				FRuneVisualSlot& Item = OutSlots.AddDefaulted_GetRef();
				Item.Component = Mesh;
				Item.MaterialIndex = Slot;
				Item.OriginalMaterial = Mesh->GetMaterial(Slot);
			}
		}
	}
	if (bRecursive)
	{
		TArray<AActor*> Children;
		Actor->GetAttachedActors(Children, true, true);
		for (AActor* Child : Children)
		{
			CollectVisualSlots(Child, false, OutSlots);
		}
	}
}

void ARune::ApplyMaterialToSlots(
	TArray<FRuneVisualSlot>& Slots,
	UMaterialInterface* Material,
	bool bFirstMaterialSlotOnly)
{
	for (FRuneVisualSlot& Slot : Slots)
	{
		if (!Slot.Component || Slot.MaterialIndex == INDEX_NONE)
		{
			continue;
		}
		if (bFirstMaterialSlotOnly && Slot.MaterialIndex != 0)
		{
			continue;
		}
		if (!Material)
		{
			Slot.Component->SetMaterial(Slot.MaterialIndex, Slot.OriginalMaterial);
			continue;
		}

		Slot.Component->SetMaterial(Slot.MaterialIndex, Material);
	}
}

void ARune::WarnForMissingVisualMaterials()
{
	if (bVisualWarningsEmitted)
	{
		return;
	}
	bVisualWarningsEmitted = true;
	TArray<FString> Missing;
	if (!VisualProfile)
	{
		Missing.Add(TEXT("VisualProfile"));
	}
	else
	{
		if (!VisualProfile->MetalMaterial) Missing.Add(TEXT("MetalMaterial"));
		if (!VisualProfile->RedActiveMaterial) Missing.Add(TEXT("RedActiveMaterial"));
		if (!VisualProfile->BlueActiveMaterial) Missing.Add(TEXT("BlueActiveMaterial"));
		if (!VisualProfile->CenterInactiveMaterial) Missing.Add(TEXT("CenterInactiveMaterial"));
		if (!VisualProfile->RedCenterActiveMaterial) Missing.Add(TEXT("RedCenterActiveMaterial"));
		if (!VisualProfile->BlueCenterActiveMaterial) Missing.Add(TEXT("BlueCenterActiveMaterial"));
		if (!VisualProfile->RedFanActiveMaterial) Missing.Add(TEXT("RedFanActiveMaterial"));
		if (!VisualProfile->BlueFanActiveMaterial) Missing.Add(TEXT("BlueFanActiveMaterial"));
		if (!VisualProfile->RedArrowMaterial) Missing.Add(TEXT("RedArrowMaterial"));
		if (!VisualProfile->BlueArrowMaterial) Missing.Add(TEXT("BlueArrowMaterial"));
		if (!VisualProfile->RedFullMaterial && !VisualProfile->RedActiveMaterial) Missing.Add(TEXT("RedFullMaterial"));
		if (!VisualProfile->BlueFullMaterial && !VisualProfile->BlueActiveMaterial) Missing.Add(TEXT("BlueFullMaterial"));
		auto ValidateRingMaterials = [&Missing](const TCHAR* Name, const TArray<TObjectPtr<UMaterialInterface>>& Materials)
		{
			if (Materials.Num() != 10)
			{
				Missing.Add(FString::Printf(TEXT("%s[%d/10]"), Name, Materials.Num()));
				return;
			}
			for (int32 Index = 0; Index < Materials.Num(); ++Index)
			{
				if (!Materials[Index])
				{
					Missing.Add(FString::Printf(TEXT("%s[%d]"), Name, Index));
				}
			}
		};
		ValidateRingMaterials(TEXT("RedFanRingMaterials"), VisualProfile->RedFanRingMaterials);
		ValidateRingMaterials(TEXT("BlueFanRingMaterials"), VisualProfile->BlueFanRingMaterials);
	}
	if (!Missing.IsEmpty())
	{
		UE_LOG(LogRuneVisual, Warning, TEXT("Rune visual profile is incomplete for %s: %s; original materials will be preserved"),
			*GetName(), *FString::Join(Missing, TEXT(", ")));
	}
}

void ARune::SetVisualCollisionResponse(AActor* Actor) const
{
	if (!Actor)
	{
		return;
	}
	TArray<UStaticMeshComponent*> Meshes;
	Actor->GetComponents<UStaticMeshComponent>(Meshes);
	for (UStaticMeshComponent* Mesh : Meshes)
	{
		Mesh->SetCollisionResponseToChannel(ECC_GameTraceChannel1, ECR_Ignore);
	}
	TArray<AActor*> Children;
	Actor->GetAttachedActors(Children, true, false);
	for (AActor* Child : Children)
	{
		SetVisualCollisionResponse(Child);
	}
}

UMaterialInterface* ARune::GetActiveMaterial() const
{
	if (!VisualProfile)
	{
		return nullptr;
	}
	return TeamColor == ETeamColor::Red
		? VisualProfile->RedActiveMaterial.Get()
		: VisualProfile->BlueActiveMaterial.Get();
}

UMaterialInterface* ARune::GetCenterMaterial(bool bInactive) const
{
	if (!VisualProfile)
	{
		return nullptr;
	}
	if (bInactive)
	{
		return VisualProfile->CenterInactiveMaterial.Get();
	}
	return TeamColor == ETeamColor::Red
		? VisualProfile->RedCenterActiveMaterial.Get()
		: VisualProfile->BlueCenterActiveMaterial.Get();
}

UMaterialInterface* ARune::GetFanActiveMaterial() const
{
	if (!VisualProfile)
	{
		return nullptr;
	}
	return TeamColor == ETeamColor::Red
		? VisualProfile->RedFanActiveMaterial.Get()
		: VisualProfile->BlueFanActiveMaterial.Get();
}

UMaterialInterface* ARune::GetFullMaterial() const
{
	if (!VisualProfile)
	{
		return nullptr;
	}
	UMaterialInterface* TeamMaterial = TeamColor == ETeamColor::Red
		? VisualProfile->RedFullMaterial.Get()
		: VisualProfile->BlueFullMaterial.Get();
	if (TeamMaterial)
	{
		return TeamMaterial;
	}
	// Completion is team-specific. The old common FullMaterial field is kept on
	// the profile for asset compatibility, but is intentionally never selected.
	return GetActiveMaterial();
}

UMaterialInterface* ARune::GetArrowMaterial() const
{
	if (!VisualProfile)
	{
		return nullptr;
	}
	return TeamColor == ETeamColor::Red
		? VisualProfile->RedArrowMaterial.Get()
		: VisualProfile->BlueArrowMaterial.Get();
}

UMaterialInterface* ARune::GetFanRingMaterial(int32 Ring) const
{
	if (!VisualProfile || Ring < 1 || Ring > 10)
	{
		return GetFullMaterial();
	}
	const TArray<TObjectPtr<UMaterialInterface>>& RingMaterials = TeamColor == ETeamColor::Red
		? VisualProfile->RedFanRingMaterials : VisualProfile->BlueFanRingMaterials;
	UMaterialInterface* RingMaterial = RingMaterials.IsValidIndex(Ring - 1)
		? RingMaterials[Ring - 1].Get() : nullptr;
	return RingMaterial ? RingMaterial : GetFullMaterial();
}
