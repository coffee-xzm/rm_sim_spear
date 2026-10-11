#include "RMSimWorldSubsystem.h"

#include "EngineUtils.h"
#include "RuneSceneDirector.h"

void URMSimWorldSubsystem::OnWorldBeginPlay(UWorld& InWorld)
{
	Super::OnWorldBeginPlay(InWorld);
	TArray<ARuneSceneDirector*> Directors;
	for (TActorIterator<ARuneSceneDirector> It(&InWorld); It; ++It)
	{
		Directors.Add(*It);
	}
	if (Directors.Num() != 1)
	{
		Director = nullptr;
		RegistrationError = FString::Printf(TEXT("Expected one ARuneSceneDirector, got %d"), Directors.Num());
		UE_LOG(LogTemp, Error, TEXT("%s"), *RegistrationError);
		return;
	}
	Director = Directors[0];
	RegistrationError.Reset();
}

void URMSimWorldSubsystem::RegisterDirector(ARuneSceneDirector* InDirector)
{
	if (!InDirector)
	{
		return;
	}
	if (!Director || Director == InDirector)
	{
		Director = InDirector;
		RegistrationError.Reset();
	}
	else
	{
		RegistrationError = TEXT("Multiple ARuneSceneDirector actors attempted registration");
		Director = nullptr;
		UE_LOG(LogTemp, Error, TEXT("%s"), *RegistrationError);
	}
}

bool URMSimWorldSubsystem::SetSceneMode(ESceneMode NewMode, int32 Seed)
{
	return Director && Director->SetSceneMode(NewMode, Seed);
}

bool URMSimWorldSubsystem::ResetMatch(int32 Seed)
{
	return Director && Director->ResetMatch(Seed);
}

bool URMSimWorldSubsystem::SetDebugOverlay(bool bEnabled)
{
	if (!Director)
	{
		return false;
	}
	Director->SetDebugOverlay(bEnabled);
	return true;
}

bool URMSimWorldSubsystem::SetAutoHitTestEnabled(bool bEnabled)
{
	if (!Director)
	{
		return false;
	}
	Director->SetAutoHitTestEnabled(bEnabled);
	return true;
}

FRunePairSnapshot URMSimWorldSubsystem::GetSnapshot() const
{
	if (Director)
	{
		return Director->GetSnapshot();
	}
	FRunePairSnapshot Result;
	Result.Error = RegistrationError.IsEmpty() ? TEXT("No ARuneSceneDirector registered") : RegistrationError;
	return Result;
}
