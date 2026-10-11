#pragma once

#include "CoreMinimal.h"
#include "RuneBindingTypes.generated.h"

USTRUCT(BlueprintType)
struct SIM_API FRuneArmBinding
{
	GENERATED_BODY()

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TObjectPtr<AActor> AxisActor = nullptr;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TObjectPtr<AActor> ArmorAnchorActor = nullptr;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TArray<TObjectPtr<AActor>> FanActors;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TArray<TObjectPtr<AActor>> FlowActors;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TArray<TObjectPtr<AActor>> LightActors;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TArray<TObjectPtr<AActor>> HorizontalLightActors;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TArray<TObjectPtr<AActor>> VerticalLightActors;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	FTransform ArmorLocalTransform = FTransform::Identity;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly, meta = (ClampMin = "0.1"))
	double ArmorRadiusCm = 5.0;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly, meta = (ClampMin = "0.1"))
	double ArmorThicknessCm = 0.4;
};

USTRUCT(BlueprintType)
struct SIM_API FRuneSideBinding
{
	GENERATED_BODY()

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TObjectPtr<AActor> VisualRootActor = nullptr;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TObjectPtr<AActor> CenterRootActor = nullptr;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	FVector RotationAxisLocal = FVector::RightVector;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	int32 RotationSign = 1;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	FTransform BaselineRelativeTransform = FTransform::Identity;

	UPROPERTY(EditInstanceOnly, BlueprintReadOnly)
	TArray<FRuneArmBinding> Arms;
};
