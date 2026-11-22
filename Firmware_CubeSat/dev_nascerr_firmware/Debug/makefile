################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (14.3.rel1)
################################################################################

-include ../makefile.init

RM := rm -rf

# All of the sources participating in the build are defined here
-include sources.mk
-include Modules/NASCERR/Utils/subdir.mk
-include Modules/NASCERR/System/subdir.mk
-include Modules/NASCERR/Sram/subdir.mk
-include Modules/NASCERR/InternalFlash/subdir.mk
-include Modules/NASCERR/Hardware/subdir.mk
-include Modules/NASCERR/ExternalFlash/subdir.mk
-include Modules/NASCERR/Experiments/subdir.mk
-include Modules/NASCERR/Database/subdir.mk
-include Modules/NASCERR/Communication/subdir.mk
-include Modules/NASCERR/Bootloader/subdir.mk
-include Modules/NASCERR/Application/subdir.mk
-include Middlewares/Third_Party/FreeRTOS/Source/portable/MemMang/subdir.mk
-include Middlewares/Third_Party/FreeRTOS/Source/portable/GCC/ARM_CM4F/subdir.mk
-include Middlewares/Third_Party/FreeRTOS/Source/CMSIS_RTOS_V2/subdir.mk
-include Middlewares/Third_Party/FreeRTOS/Source/subdir.mk
-include Drivers/STM32L4xx_HAL_Driver/Src/subdir.mk
-include Core/Startup/subdir.mk
-include Core/Src/subdir.mk
-include objects.mk

ifneq ($(MAKECMDGOALS),clean)
ifneq ($(strip $(S_DEPS)),)
-include $(S_DEPS)
endif
ifneq ($(strip $(S_UPPER_DEPS)),)
-include $(S_UPPER_DEPS)
endif
ifneq ($(strip $(C_DEPS)),)
-include $(C_DEPS)
endif
endif

-include ../makefile.defs

OPTIONAL_TOOL_DEPS := \
$(wildcard ../makefile.defs) \
$(wildcard ../makefile.init) \
$(wildcard ../makefile.targets) \


BUILD_ARTIFACT_NAME := dev_nascerr_firmware
BUILD_ARTIFACT_EXTENSION := elf
BUILD_ARTIFACT_PREFIX :=
BUILD_ARTIFACT := $(BUILD_ARTIFACT_PREFIX)$(BUILD_ARTIFACT_NAME)$(if $(BUILD_ARTIFACT_EXTENSION),.$(BUILD_ARTIFACT_EXTENSION),)

# Add inputs and outputs from these tool invocations to the build variables 
EXECUTABLES += \
dev_nascerr_firmware.elf \

MAP_FILES += \
dev_nascerr_firmware.map \

SIZE_OUTPUT += \
default.size.stdout \

OBJDUMP_LIST += \
dev_nascerr_firmware.list \

OBJCOPY_BIN += \
dev_nascerr_firmware.bin \


# All Target
all: main-build

# Main-build Target
main-build: dev_nascerr_firmware.elf secondary-outputs

# Tool invocations
dev_nascerr_firmware.elf dev_nascerr_firmware.map: $(OBJS) $(USER_OBJS) C:\Users\cicer\OneDrive\Documents\GitHub\Projeto_NASCERR_UFC\Firmware_CubeSat\dev_nascerr_firmware\STM32L4S5QIIX_FLASH.ld makefile objects.list $(OPTIONAL_TOOL_DEPS)
	arm-none-eabi-gcc -o "dev_nascerr_firmware.elf" @"objects.list" $(USER_OBJS) $(LIBS) -mcpu=cortex-m4 -T"C:\Users\cicer\OneDrive\Documents\GitHub\Projeto_NASCERR_UFC\Firmware_CubeSat\dev_nascerr_firmware\STM32L4S5QIIX_FLASH.ld" --specs=nosys.specs -Wl,-Map="dev_nascerr_firmware.map" -Wl,--gc-sections -static --specs=nano.specs -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -u _printf_float -Wl,--start-group -lc -lm -Wl,--end-group
	@echo 'Finished building target: $@'
	@echo ' '

default.size.stdout: $(EXECUTABLES) makefile objects.list $(OPTIONAL_TOOL_DEPS)
	arm-none-eabi-size  $(EXECUTABLES)
	@echo 'Finished building: $@'
	@echo ' '

dev_nascerr_firmware.list: $(EXECUTABLES) makefile objects.list $(OPTIONAL_TOOL_DEPS)
	arm-none-eabi-objdump -h -S $(EXECUTABLES) > "dev_nascerr_firmware.list"
	@echo 'Finished building: $@'
	@echo ' '

dev_nascerr_firmware.bin: $(EXECUTABLES) makefile objects.list $(OPTIONAL_TOOL_DEPS)
	arm-none-eabi-objcopy  -O binary $(EXECUTABLES) "dev_nascerr_firmware.bin"
	@echo 'Finished building: $@'
	@echo ' '

# Other Targets
clean:
	-$(RM) default.size.stdout dev_nascerr_firmware.bin dev_nascerr_firmware.elf dev_nascerr_firmware.list dev_nascerr_firmware.map
	-@echo ' '

secondary-outputs: $(SIZE_OUTPUT) $(OBJDUMP_LIST) $(OBJCOPY_BIN)

fail-specified-linker-script-missing:
	@echo 'Error: Cannot find the specified linker script. Check the linker settings in the build configuration.'
	@exit 2

warn-no-linker-script-specified:
	@echo 'Warning: No linker script specified. Check the linker settings in the build configuration.'

.PHONY: all clean dependents main-build fail-specified-linker-script-missing warn-no-linker-script-specified

-include ../makefile.targets
